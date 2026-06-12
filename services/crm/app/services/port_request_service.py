"""v0.17 — PortRequest service (MNP, operator-driven).

Two flows hang off ``approve``:

* **Port-in** — donor MSISDN gets seeded into ``inventory.msisdn_pool``
  and either (a) assigned directly to ``target_subscription_id`` if the
  operator already created the subscription, or (b) left as
  ``available`` for normal signup to consume. ON CONFLICT skip — if the
  number is already in our pool (e.g. a previous port-in or a stale
  range-add), the existing row is preserved.
* **Port-out** — the existing subscription's MSISDN flips to terminal
  ``ported_out`` (with ``quarantine_until = '9999-12-31'`` so reserve
  predicates skip it forever), then the subscription is terminated via
  ``SubscriptionClient.terminate(release_inventory=False)`` so the
  MSISDN release step is skipped. eSIM still recycles.

The ``validated`` FSM state is a hook for an automated donor-carrier
check; v0.17 ships only the operator-driven path so ``approve`` collapses
``requested → completed`` directly.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import structlog
from bss_clients import SubscriptionClient
from bss_clock import now as clock_now
from bss_models.crm import PortRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.events import publisher
from app.policies import port_request as pr_policies
from app.policies.base import PolicyViolation
from app.repositories.msisdn_repo import MsisdnRepository
from app.repositories.port_request_repo import PortRequestRepository

log = structlog.get_logger()


def _next_id() -> str:
    return f"PORT-{uuid4().hex[:8].upper()}"


class PortRequestService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        port_repo: PortRequestRepository,
        msisdn_repo: MsisdnRepository,
        subscription_client: SubscriptionClient,
    ) -> None:
        self._session = session
        self._port_repo = port_repo
        self._msisdn_repo = msisdn_repo
        self._subscription = subscription_client

    async def list_requests(
        self,
        *,
        state: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PortRequest]:
        return await self._port_repo.list_requests(
            state=state, direction=direction, limit=limit, offset=offset
        )

    async def get(self, port_id: str) -> PortRequest | None:
        return await self._port_repo.get(port_id)

    async def create(
        self,
        *,
        direction: str,
        donor_carrier: str,
        donor_msisdn: str,
        target_subscription_id: str | None,
        requested_port_date: date,
    ) -> PortRequest:
        ctx = auth_context.current()
        pr_policies.check_direction_valid(direction)
        pr_policies.check_target_sub_required(direction, target_subscription_id)
        existing = await self._port_repo.get_active_for_donor(
            donor_msisdn, ctx.tenant
        )
        pr_policies.check_donor_msisdn_unique(donor_msisdn, existing)

        port = PortRequest(
            id=_next_id(),
            direction=direction,
            donor_carrier=donor_carrier,
            donor_msisdn=donor_msisdn,
            target_subscription_id=target_subscription_id,
            requested_port_date=requested_port_date,
            state="requested",
        )
        await self._port_repo.create(port)
        await publisher.publish(
            self._session,
            event_type="port_request.created",
            aggregate_type="port_request",
            aggregate_id=port.id,
            payload={
                "portRequestId": port.id,
                "direction": direction,
                "donorCarrier": donor_carrier,
                "donorMsisdn": donor_msisdn,
                "targetSubscriptionId": target_subscription_id,
                "requestedPortDate": requested_port_date.isoformat(),
            },
        )
        await self._session.commit()
        log.info(
            "port_request.created",
            port_request_id=port.id,
            direction=direction,
            donor_msisdn=donor_msisdn,
        )
        return port

    async def approve(self, port_id: str) -> PortRequest:
        port = await self._port_repo.get(port_id)
        if not port:
            raise PolicyViolation(
                rule="port_request.not_found",
                message=f"Port request {port_id} not found",
                context={"port_request_id": port_id},
            )
        pr_policies.check_transition_valid(port.state, "complete")

        if port.direction == "port_in":
            await self._approve_port_in(port)
        else:
            await self._approve_port_out(port)

        port.state = "completed"
        await self._port_repo.update(port)
        await publisher.publish(
            self._session,
            event_type="port_request.approved",
            aggregate_type="port_request",
            aggregate_id=port.id,
            payload={
                "portRequestId": port.id,
                "direction": port.direction,
                "donorMsisdn": port.donor_msisdn,
                "targetSubscriptionId": port.target_subscription_id,
            },
        )
        await publisher.publish(
            self._session,
            event_type="port_request.completed",
            aggregate_type="port_request",
            aggregate_id=port.id,
            payload={"portRequestId": port.id, "direction": port.direction},
        )
        await self._session.commit()
        log.info(
            "port_request.approved",
            port_request_id=port.id,
            direction=port.direction,
        )
        return port

    async def reject(self, port_id: str, reason: str) -> PortRequest:
        port = await self._port_repo.get(port_id)
        if not port:
            raise PolicyViolation(
                rule="port_request.not_found",
                message=f"Port request {port_id} not found",
                context={"port_request_id": port_id},
            )
        pr_policies.check_reject_reason(reason)
        pr_policies.check_transition_valid(port.state, "reject")

        port.state = "rejected"
        port.rejection_reason = reason
        await self._port_repo.update(port)
        await publisher.publish(
            self._session,
            event_type="port_request.rejected",
            aggregate_type="port_request",
            aggregate_id=port.id,
            payload={
                "portRequestId": port.id,
                "direction": port.direction,
                "donorMsisdn": port.donor_msisdn,
                "reason": reason,
            },
        )
        await self._session.commit()
        log.info(
            "port_request.rejected",
            port_request_id=port.id,
            reason=reason,
        )
        return port

    # ── port-in ────────────────────────────────────────────────────

    async def _approve_port_in(self, port: PortRequest) -> None:
        """Seed the donor MSISDN into the pool.

        Atomic via ``ON CONFLICT (msisdn) DO NOTHING`` — if a previous
        port-in (or a range-add that overlapped) already inserted the
        row, the prior state wins. The conflict-skip path is a no-op,
        not an error: the operator just sees the donor msisdn already
        in our records (e.g. a re-port).
        """
        ctx = auth_context.current()
        target_sub = port.target_subscription_id
        status = "assigned" if target_sub else "available"
        await self._session.execute(
            text(
                """
                INSERT INTO inventory.msisdn_pool
                    (msisdn, status, assigned_to_subscription_id, tenant_id)
                VALUES (:m, :st, :sub, :t)
                ON CONFLICT (msisdn) DO NOTHING
                """
            ),
            {
                "m": port.donor_msisdn,
                "st": status,
                "sub": target_sub,
                "t": ctx.tenant,
            },
        )
        await publisher.publish(
            self._session,
            event_type="inventory.msisdn.seeded_from_port_in",
            aggregate_type="msisdn_pool",
            aggregate_id=port.donor_msisdn,
            payload={
                "msisdn": port.donor_msisdn,
                "donorCarrier": port.donor_carrier,
                "portRequestId": port.id,
                "targetSubscriptionId": target_sub,
                "status": status,
            },
        )

    # ── port-out ───────────────────────────────────────────────────

    async def _approve_port_out(self, port: PortRequest) -> None:
        """Flip the donor MSISDN to terminal ``ported_out`` and
        terminate the target subscription with ``release_inventory=False``.

        Doctrine v0.17+: ``ported_out`` is terminal. Subscription
        terminate's eSIM recycle still runs. The order matters — flip
        first so a concurrent reserve-next can't grab the number
        between subscription terminate and the flip.
        """
        if not port.target_subscription_id:
            raise PolicyViolation(
                rule="port_request.create.target_sub_required_for_port_out",
                message="port_out requires target_subscription_id at approve",
                context={"port_request_id": port.id},
            )

        await self._msisdn_repo.mark_ported_out(
            port.donor_msisdn,
            subscription_id=port.target_subscription_id,
        )
        await publisher.publish(
            self._session,
            event_type="inventory.msisdn.ported_out",
            aggregate_type="msisdn_pool",
            aggregate_id=port.donor_msisdn,
            payload={
                "msisdn": port.donor_msisdn,
                "donorCarrier": port.donor_carrier,
                "portRequestId": port.id,
                "targetSubscriptionId": port.target_subscription_id,
                "portedOutAt": clock_now().isoformat(),
            },
        )

        try:
            await self._subscription.terminate(
                port.target_subscription_id,
                reason="ported_out",
                release_inventory=False,
            )
        except Exception:
            log.warning(
                "port_request.subscription_terminate_failed",
                subscription_id=port.target_subscription_id,
            )
