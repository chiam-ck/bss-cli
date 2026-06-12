"""Customer service — orchestrates policies, repos, events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from bss_clock import now as clock_now
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.events import publisher
from app.policies import customer as customer_policies
from app.repositories.customer_repo import CustomerRepository
from app.repositories.interaction_repo import InteractionRepository
from app.repositories.msisdn_repo import MsisdnRepository
from bss_models.crm import ContactMedium, Customer, Individual, Interaction, Party

if TYPE_CHECKING:
    from bss_clients.subscription import SubscriptionClient

log = structlog.get_logger()


def _next_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


class CustomerService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        customer_repo: CustomerRepository,
        interaction_repo: InteractionRepository,
        subscription_client: SubscriptionClient | None = None,
        msisdn_repo: MsisdnRepository | None = None,
        loyalty_client=None,
    ) -> None:
        self._session = session
        self._customer_repo = customer_repo
        self._interaction_repo = interaction_repo
        self._subscription_client = subscription_client
        self._msisdn_repo = msisdn_repo
        self._loyalty_client = loyalty_client

    async def create_customer(
        self,
        *,
        given_name: str,
        family_name: str,
        date_of_birth: str | None = None,
        contact_mediums: list[dict],
    ) -> Customer:
        ctx = auth_context.current()

        # --- Policies ---
        customer_policies.check_requires_contact_medium(contact_mediums)

        emails = [cm["value"] for cm in contact_mediums if cm.get("medium_type") == "email"]
        for email in emails:
            await customer_policies.check_email_unique(email, self._customer_repo)

        # --- Create aggregate ---
        now = clock_now()
        party_id = _next_id("PTY")
        customer_id = _next_id("CUST")

        party = Party(id=party_id, party_type="individual", tenant_id=ctx.tenant)
        await self._customer_repo.create_party(party)

        dob = None
        if date_of_birth:
            from datetime import date as date_type
            dob = date_type.fromisoformat(date_of_birth)
        individual = Individual(
            party_id=party_id,
            given_name=given_name,
            family_name=family_name,
            date_of_birth=dob,
            tenant_id=ctx.tenant,
        )
        await self._customer_repo.create_individual(individual)

        customer = Customer(
            id=customer_id,
            party_id=party_id,
            status="active",
            customer_since=now,
            tenant_id=ctx.tenant,
        )
        await self._customer_repo.create_customer(customer)

        for cm_data in contact_mediums:
            cm = ContactMedium(
                id=_next_id("CM"),
                party_id=party_id,
                medium_type=cm_data["medium_type"],
                value=cm_data["value"],
                is_primary=cm_data.get("is_primary", False),
                valid_from=now,
                tenant_id=ctx.tenant,
            )
            await self._customer_repo.create_contact_medium(cm)

        # --- Event ---
        await publisher.publish(
            self._session,
            event_type="customer.created",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={"given_name": given_name, "family_name": family_name},
        )

        # --- Interaction auto-log ---
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Customer created: {given_name} {family_name}",
                occurred_at=now,
                tenant_id=ctx.tenant,
            )
        )

        await self._session.commit()

        # v1.1.1 — mirror the new customer into loyalty's registry so its
        # customer-facing views recognise the id. Best-effort: a loyalty hiccup
        # must never fail customer creation (the backfill command reconciles
        # drift). Idempotent on the customer id.
        if self._loyalty_client is not None:
            try:
                await self._loyalty_client.register_customer(customer_id=customer_id)
            except Exception:  # noqa: BLE001 — sync is non-critical to CRM
                log.warning(
                    "crm.customer.loyalty_register_failed",
                    customer_id=customer_id,
                    exc_info=True,
                )
        return customer

    async def get_customer(self, customer_id: str) -> Customer | None:
        return await self._customer_repo.get_with_party(customer_id)

    async def find_by_msisdn(self, msisdn: str) -> Customer | None:
        """Resolve MSISDN → subscription → customer.

        Inventory's MSISDN pool stores ``assigned_to_subscription_id``;
        the subscription's ``customerId`` is then the owner. Returns
        ``None`` if any hop misses (number unassigned, or assigned to
        a subscription whose customer was deleted).
        """
        if self._msisdn_repo is None or self._subscription_client is None:
            raise RuntimeError(
                "find_by_msisdn requires msisdn_repo + subscription_client"
            )
        row = await self._msisdn_repo.get(msisdn)
        if row is None or not row.assigned_to_subscription_id:
            return None
        try:
            subscription = await self._subscription_client.get(
                row.assigned_to_subscription_id
            )
        except Exception:  # noqa: BLE001 — best-effort cross-service read
            return None
        customer_id = subscription.get("customerId") if subscription else None
        if not customer_id:
            return None
        return await self._customer_repo.get_with_party(customer_id)

    async def find_by_email(self, email: str) -> Customer | None:
        """Resolve an email address → customer via active contact mediums.

        Exact match on a live (``valid_to IS NULL``) email medium —
        the same lookup ``bss_portal_auth`` uses for login identity.
        Returns ``None`` when no customer carries the address.
        """
        cust = await self._customer_repo.find_by_email(email)
        if cust is None:
            return None
        return await self._customer_repo.get_with_party(cust.id)

    async def list_customers(
        self,
        *,
        status: str | None = None,
        name_contains: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Customer]:
        return await self._customer_repo.list_customers(
            status=status,
            name_contains=name_contains,
            limit=limit,
            offset=offset,
        )

    async def update_customer(self, customer_id: str, **updates: str | None) -> Customer:
        cust = await self._customer_repo.get(customer_id)
        if not cust:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.update.not_found",
                message=f"Customer {customer_id} not found",
                context={"customer_id": customer_id},
            )

        # Policy: cannot deactivate a customer with active subscriptions
        new_status = updates.get("status")
        if (
            new_status
            and new_status != "active"
            and cust.status == "active"
            and self._subscription_client
        ):
            await customer_policies.check_no_active_subscriptions(
                customer_id, self._subscription_client
            )

        for k, v in updates.items():
            if hasattr(cust, k) and v is not None:
                setattr(cust, k, v)
        await self._customer_repo.update(cust)

        ctx = auth_context.current()
        await publisher.publish(
            self._session,
            event_type="customer.updated",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={"fields_updated": list(updates.keys())},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Customer updated: {', '.join(updates.keys())}",
                occurred_at=clock_now(),
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return cust

    async def add_contact_medium(
        self, customer_id: str, *, medium_type: str, value: str, is_primary: bool = False
    ) -> ContactMedium:
        cust = await self._customer_repo.get(customer_id)
        if not cust:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact.not_found",
                message=f"Customer {customer_id} not found",
                context={"customer_id": customer_id},
            )
        if medium_type == "email":
            await customer_policies.check_email_unique(value, self._customer_repo)

        ctx = auth_context.current()
        cm = ContactMedium(
            id=_next_id("CM"),
            party_id=cust.party_id,
            medium_type=medium_type,
            value=value,
            is_primary=is_primary,
            valid_from=clock_now(),
            tenant_id=ctx.tenant,
        )
        await self._customer_repo.create_contact_medium(cm)

        await publisher.publish(
            self._session,
            event_type="customer.contact_medium_added",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={"medium_type": medium_type},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Contact medium added: {medium_type}",
                occurred_at=clock_now(),
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return cm

    async def update_individual_name(
        self,
        customer_id: str,
        *,
        given_name: str | None = None,
        family_name: str | None = None,
    ):
        """v0.10 — update the customer's display name on /profile/contact.

        At least one of ``given_name`` / ``family_name`` must be provided;
        callers passing both empty get a no-op + the current row back.
        """
        if given_name is None and family_name is None:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.individual.update.no_fields",
                message="At least one of given_name or family_name is required",
                context={"customer_id": customer_id},
            )

        cust = await self._customer_repo.get(customer_id)
        if not cust:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact.not_found",
                message=f"Customer {customer_id} not found",
                context={"customer_id": customer_id},
            )
        ind = await self._customer_repo.get_individual_for_party(cust.party_id)
        if ind is None:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.individual.not_found",
                message=f"Customer {customer_id} has no individual record",
                context={"customer_id": customer_id},
            )

        await self._customer_repo.update_individual_name(
            ind, given_name=given_name, family_name=family_name
        )

        ctx = auth_context.current()
        await publisher.publish(
            self._session,
            event_type="customer.individual_updated",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={"fields": [
                f for f in ("given_name", "family_name")
                if locals().get(f) is not None
            ]},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary="Customer updated their display name",
                occurred_at=clock_now(),
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return ind

    async def update_contact_medium(
        self, customer_id: str, cm_id: str, *, value: str
    ) -> ContactMedium:
        """Update the ``value`` of an existing contact medium.

        v0.10 — phone / address updates from the self-serve portal go
        through this method. Email updates do NOT — they go through
        ``bss_portal_auth.email_change`` so the CRM update + the
        ``portal_auth.identity.email`` update commit atomically.
        Calling this method with ``cm.medium_type == 'email'`` raises
        ``policy.customer.contact_medium.email_must_use_change_flow``.
        """
        cust = await self._customer_repo.get(customer_id)
        if not cust:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact.not_found",
                message=f"Customer {customer_id} not found",
                context={"customer_id": customer_id},
            )
        cm = await self._customer_repo.get_contact_medium(cm_id)
        if not cm or cm.party_id != cust.party_id:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact_medium.unknown",
                message=f"Contact medium {cm_id} not found for customer {customer_id}",
                context={"customer_id": customer_id, "cm_id": cm_id},
            )
        if cm.medium_type == "email":
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact_medium.email_must_use_change_flow",
                message=(
                    "Email updates must use the verified email-change flow "
                    "(start_email_change → verify_email_change), not the "
                    "direct contact-medium update."
                ),
                context={"customer_id": customer_id, "cm_id": cm_id},
            )

        await self._customer_repo.update_contact_medium_value(cm, value)

        ctx = auth_context.current()
        await publisher.publish(
            self._session,
            event_type="customer.contact_medium_updated",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={"cm_id": cm_id, "medium_type": cm.medium_type},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Contact medium updated: {cm.medium_type}",
                occurred_at=clock_now(),
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return cm

    async def remove_contact_medium(self, customer_id: str, cm_id: str) -> None:
        cust = await self._customer_repo.get(customer_id)
        if not cust:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact.not_found",
                message=f"Customer {customer_id} not found",
                context={"customer_id": customer_id},
            )
        cm = await self._customer_repo.get_contact_medium(cm_id)
        if not cm or cm.party_id != cust.party_id:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.contact.medium_not_found",
                message=f"Contact medium {cm_id} not found for customer {customer_id}",
                context={"customer_id": customer_id, "cm_id": cm_id},
            )
        await self._customer_repo.delete_contact_medium(cm)

        ctx = auth_context.current()
        await publisher.publish(
            self._session,
            event_type="customer.contact_medium_removed",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={"cm_id": cm_id},
        )
        await self._session.commit()
