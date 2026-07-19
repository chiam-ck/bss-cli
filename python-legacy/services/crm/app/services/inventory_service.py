"""Inventory service — MSISDN + eSIM pool management."""

import os

import structlog
from bss_models.inventory import EsimProfile, MsisdnPool
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.domain import esim_state
from app.events import publisher
from app.policies import inventory as inv_policies
from app.policies.base import PolicyViolation
from app.repositories.esim_repo import EsimRepository
from app.repositories.msisdn_repo import MsisdnRepository

log = structlog.get_logger()


def _low_watermark_threshold() -> int:
    """Configured floor below which the pool emits ``inventory.msisdn.pool_low``.

    Read at every check so an ops-side `.env` bump takes effect on next
    reservation without a service restart — the value is non-secret and
    safe to re-evaluate cheaply.
    """
    raw = os.environ.get("BSS_INVENTORY_MSISDN_POOL_LOW_THRESHOLD", "50")
    try:
        return int(raw)
    except ValueError:
        return 50


class InventoryService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        msisdn_repo: MsisdnRepository,
        esim_repo: EsimRepository,
    ) -> None:
        self._session = session
        self._msisdn_repo = msisdn_repo
        self._esim_repo = esim_repo

    # ── MSISDN ──────────────────────────────────────────────────────

    async def list_msisdns(
        self,
        *,
        status: str | None = None,
        prefix: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MsisdnPool]:
        return await self._msisdn_repo.list_msisdns(
            status=status, prefix=prefix, limit=limit, offset=offset
        )

    async def count_msisdns(
        self, *, prefix: str | None = None
    ) -> dict[str, int]:
        ctx = auth_context.current()
        return await self._msisdn_repo.count_by_status(
            tenant_id=ctx.tenant, prefix=prefix
        )

    async def get_msisdn(self, msisdn: str) -> MsisdnPool | None:
        return await self._msisdn_repo.get(msisdn)

    async def reserve_msisdn(self, msisdn: str) -> MsisdnPool:
        ctx = auth_context.current()
        result = await self._msisdn_repo.reserve_atomic(msisdn, ctx.tenant)
        if not result:
            raise PolicyViolation(
                rule="msisdn.reserve.status_must_be_available",
                message=f"MSISDN {msisdn} is not available for reservation",
                context={"msisdn": msisdn},
            )
        await self._session.commit()
        await self._maybe_emit_pool_low(ctx.tenant)
        return result

    async def reserve_next_msisdn(self, preference: str | None = None) -> MsisdnPool:
        """Reserve next available MSISDN, or a preferred one if specified."""
        ctx = auth_context.current()
        if preference:
            result = await self._msisdn_repo.reserve_atomic(preference, ctx.tenant)
        else:
            result = await self._msisdn_repo.reserve_next_available(ctx.tenant)
        if not result:
            raise PolicyViolation(
                rule="msisdn.reserve.no_available",
                message="No MSISDN available matching criteria",
                context={"preference": preference},
            )
        await self._session.commit()
        await self._maybe_emit_pool_low(ctx.tenant)
        return result

    async def add_msisdn_range(
        self, *, prefix: str, count: int
    ) -> dict[str, str | int]:
        """v0.17 — bulk-extend the pool by a contiguous range.

        Operator-only via cockpit/REPL. Policies bound the inputs; the
        repo's ``ON CONFLICT DO NOTHING`` makes overlapping calls
        idempotent (prior rows — including ``ported_out`` ones — are
        preserved). Emits ``inventory.msisdn.range_added`` for cockpit
        observability.
        """
        inv_policies.check_sane_prefix(prefix, count)
        ctx = auth_context.current()
        inserted, skipped = await self._msisdn_repo.add_range(
            prefix, count, ctx.tenant
        )
        first = f"{prefix}{0:04d}"
        last = f"{prefix}{count - 1:04d}"
        await publisher.publish(
            self._session,
            event_type="inventory.msisdn.range_added",
            aggregate_type="msisdn_pool",
            aggregate_id=prefix,
            payload={
                "prefix": prefix,
                "count": count,
                "inserted": inserted,
                "skipped": skipped,
                "first": first,
                "last": last,
            },
        )
        await self._session.commit()
        log.info(
            "inventory.msisdn.range_added",
            prefix=prefix,
            count=count,
            inserted=inserted,
            skipped=skipped,
        )
        return {
            "prefix": prefix,
            "count": count,
            "inserted": inserted,
            "skipped": skipped,
            "first": first,
            "last": last,
        }

    async def _maybe_emit_pool_low(self, tenant: str) -> None:
        """Post-commit fire-and-forget: emit pool-low if available count
        crossed the threshold. Errors swallowed — this is an
        observability nudge, not a transactional invariant."""
        try:
            available = await self._msisdn_repo.count_available(tenant)
            threshold = _low_watermark_threshold()
            if available <= threshold:
                await publisher.publish(
                    self._session,
                    event_type="inventory.msisdn.pool_low",
                    aggregate_type="msisdn_pool",
                    aggregate_id="DEFAULT",
                    payload={"available": available, "threshold": threshold},
                )
                await self._session.commit()
                log.warning(
                    "inventory.msisdn.pool_low",
                    available=available,
                    threshold=threshold,
                )
        except Exception:
            log.exception("inventory.msisdn.pool_low.emit_failed")

    async def assign_msisdn(self, msisdn: str, subscription_id: str | None = None) -> MsisdnPool:
        row = await self._msisdn_repo.get(msisdn)
        if not row:
            raise PolicyViolation(
                rule="msisdn.assign.not_found",
                message=f"MSISDN {msisdn} not found",
                context={"msisdn": msisdn},
            )
        if row.status not in ("reserved", "assigned"):
            raise PolicyViolation(
                rule="msisdn.assign.must_be_reserved",
                message=f"MSISDN {msisdn} must be reserved before assignment",
                context={"msisdn": msisdn, "status": row.status},
            )
        row.status = "assigned"
        if subscription_id:
            row.assigned_to_subscription_id = subscription_id
        await self._msisdn_repo.update_status(msisdn, "assigned")
        await self._session.commit()
        return row

    async def release_msisdn(self, msisdn: str) -> MsisdnPool:
        row = await self._msisdn_repo.get(msisdn)
        if not row:
            raise PolicyViolation(
                rule="msisdn.release.not_found",
                message=f"MSISDN {msisdn} not found",
                context={"msisdn": msisdn},
            )
        inv_policies.check_msisdn_releasable(row.status, msisdn)
        result = await self._msisdn_repo.update_status(msisdn, "available")
        await self._session.commit()
        return result

    # ── eSIM ────────────────────────────────────────────────────────

    async def list_esims(
        self, *, status: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[EsimProfile]:
        return await self._esim_repo.list_esims(
            status=status, limit=limit, offset=offset
        )

    async def get_esim(self, iccid: str) -> EsimProfile | None:
        return await self._esim_repo.get(iccid)

    async def reserve_esim(self) -> EsimProfile:
        ctx = auth_context.current()
        result = await self._esim_repo.reserve_next_available(ctx.tenant)
        if not result:
            raise PolicyViolation(
                rule="esim.reserve.status_must_be_available",
                message="No eSIM profile available for reservation",
                context={},
            )
        await self._session.commit()
        return result

    async def assign_msisdn_to_esim(self, iccid: str, msisdn: str) -> EsimProfile:
        esim = await self._esim_repo.get(iccid)
        if not esim:
            raise PolicyViolation(
                rule="esim.not_found",
                message=f"eSIM {iccid} not found",
                context={"iccid": iccid},
            )
        if esim.profile_state != "reserved":
            raise PolicyViolation(
                rule="esim.assign_msisdn.esim_must_be_reserved",
                message=f"eSIM {iccid} must be reserved to assign MSISDN",
                context={"iccid": iccid, "state": esim.profile_state},
            )
        msisdn_row = await self._msisdn_repo.get(msisdn)
        if not msisdn_row:
            raise PolicyViolation(
                rule="esim.assign_msisdn.msisdn_not_found",
                message=f"MSISDN {msisdn} not found",
                context={"msisdn": msisdn},
            )
        inv_policies.check_msisdn_reserved_for_assign(msisdn_row.status, msisdn)

        result = await self._esim_repo.update_state(iccid, "reserved", assigned_msisdn=msisdn)
        await self._session.commit()
        return result

    async def _transition_esim(self, iccid: str, trigger: str) -> EsimProfile:
        esim = await self._esim_repo.get(iccid)
        if not esim:
            raise PolicyViolation(
                rule="esim.not_found",
                message=f"eSIM {iccid} not found",
                context={"iccid": iccid},
            )
        if not esim_state.is_valid_transition(esim.profile_state, trigger):
            raise PolicyViolation(
                rule="esim.transition.invalid",
                message=f"Cannot '{trigger}' eSIM from state '{esim.profile_state}'",
                context={"iccid": iccid, "state": esim.profile_state, "trigger": trigger},
            )
        new_state = esim_state.get_next_state(esim.profile_state, trigger)
        result = await self._esim_repo.update_state(iccid, new_state)
        await self._session.commit()
        return result

    async def mark_downloaded(self, iccid: str) -> EsimProfile:
        return await self._transition_esim(iccid, "download")

    async def mark_activated(self, iccid: str) -> EsimProfile:
        return await self._transition_esim(iccid, "activate")

    async def recycle_esim(self, iccid: str) -> EsimProfile:
        return await self._transition_esim(iccid, "recycle")

    async def release_esim(self, iccid: str) -> EsimProfile:
        return await self._transition_esim(iccid, "release")

    async def get_activation_code(self, iccid: str) -> dict:
        esim = await self._esim_repo.get(iccid)
        if not esim:
            raise PolicyViolation(
                rule="esim.not_found",
                message=f"eSIM {iccid} not found",
                context={"iccid": iccid},
            )
        return {
            "iccid": esim.iccid,
            "activation_code": esim.activation_code,
            "smdp_server": esim.smdp_server,
            "matching_id": esim.matching_id,
        }
