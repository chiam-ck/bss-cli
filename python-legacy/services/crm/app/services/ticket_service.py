"""Ticket service — orchestrates policies, repos, events."""

from uuid import uuid4

import structlog
from bss_clock import now as clock_now
from bss_models.crm import Interaction, Ticket, TicketStateHistory
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.domain import ticket_state
from app.events import publisher
from app.policies import ticket as ticket_policies
from app.repositories.case_repo import CaseRepository
from app.repositories.customer_repo import CustomerRepository
from app.repositories.interaction_repo import InteractionRepository
from app.repositories.ticket_repo import TicketRepository

log = structlog.get_logger()


def _next_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


class TicketService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        ticket_repo: TicketRepository,
        customer_repo: CustomerRepository,
        case_repo: CaseRepository,
        interaction_repo: InteractionRepository,
    ) -> None:
        self._session = session
        self._ticket_repo = ticket_repo
        self._customer_repo = customer_repo
        self._case_repo = case_repo
        self._interaction_repo = interaction_repo

    async def open_ticket(
        self,
        *,
        customer_id: str,
        subject: str,
        description: str | None = None,
        ticket_type: str = "information_request",
        priority: str = "normal",
        case_id: str | None = None,
        assigned_to_agent_id: str | None = None,
    ) -> Ticket:
        ctx = auth_context.current()

        await ticket_policies.check_customer_exists(customer_id, self._customer_repo)

        if assigned_to_agent_id:
            await ticket_policies.check_agent_active(assigned_to_agent_id, self._customer_repo)

        if case_id:
            case = await self._case_repo.get(case_id)
            if not case:
                from app.policies.base import PolicyViolation
                raise PolicyViolation(
                    rule="ticket.open.case_not_found",
                    message=f"Case {case_id} not found",
                    context={"case_id": case_id},
                )

        now = clock_now()
        ticket_id = _next_id("TKT")
        ticket = Ticket(
            id=ticket_id,
            case_id=case_id,
            customer_id=customer_id,
            ticket_type=ticket_type,
            subject=subject,
            description=description,
            state="open",
            priority=priority,
            assigned_to_agent_id=assigned_to_agent_id,
            opened_at=now,
            tenant_id=ctx.tenant,
        )
        await self._ticket_repo.create(ticket)

        await self._ticket_repo.add_state_history(
            TicketStateHistory(
                ticket_id=ticket_id,
                from_state=None,
                to_state="open",
                reason="Ticket created",
                tenant_id=ctx.tenant,
            )
        )

        await publisher.publish(
            self._session,
            event_type="ticket.opened",
            aggregate_type="ticket",
            aggregate_id=ticket_id,
            payload={
                "customer_id": customer_id,
                "case_id": case_id,
                "subject": subject,
            },
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Ticket opened: {subject}",
                related_ticket_id=ticket_id,
                related_case_id=case_id,
                occurred_at=now,
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return ticket

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        return await self._ticket_repo.get(ticket_id)

    async def list_tickets(
        self,
        *,
        customer_id: str | None = None,
        case_id: str | None = None,
        state: str | None = None,
        assigned_to_agent_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Ticket]:
        return await self._ticket_repo.list_tickets(
            customer_id=customer_id,
            case_id=case_id,
            state=state,
            assigned_to_agent_id=assigned_to_agent_id,
            limit=limit,
            offset=offset,
        )

    async def transition_ticket(
        self, ticket_id: str, trigger: str, **kwargs: str | None
    ) -> Ticket:
        ctx = auth_context.current()
        ticket = await self._ticket_repo.get(ticket_id)
        if not ticket:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="ticket.not_found",
                message=f"Ticket {ticket_id} not found",
                context={"ticket_id": ticket_id},
            )

        # Trigger-specific validations
        if trigger == "cancel":
            ticket_policies.check_cancel_allowed(ticket.state)
        else:
            ticket_policies.check_ticket_transition(ticket.state, trigger)

        if trigger == "ack" and not ticket.assigned_to_agent_id:
            agent_id = kwargs.get("assigned_to_agent_id")
            if not agent_id:
                from app.policies.base import PolicyViolation
                raise PolicyViolation(
                    rule="ticket.ack.requires_agent",
                    message="Acknowledging requires an assigned agent",
                    context={"ticket_id": ticket_id},
                )
            await ticket_policies.check_agent_active(agent_id, self._customer_repo)
            ticket.assigned_to_agent_id = agent_id

        if trigger == "resolve":
            resolution_notes = kwargs.get("resolution_notes")
            ticket_policies.check_resolution_notes(resolution_notes)
            ticket.resolution_notes = resolution_notes
            ticket.resolved_at = clock_now()

        # Apply
        old_state = ticket.state
        new_state = ticket_state.get_next_state(ticket.state, trigger)
        ticket.state = new_state

        if new_state in ("closed", "cancelled"):
            ticket.closed_at = clock_now()

        await self._ticket_repo.update(ticket)

        await self._ticket_repo.add_state_history(
            TicketStateHistory(
                ticket_id=ticket_id,
                from_state=old_state,
                to_state=new_state,
                changed_by_agent_id=kwargs.get("agent_id") or ticket.assigned_to_agent_id,
                reason=kwargs.get("reason", f"Trigger: {trigger}"),
                tenant_id=ctx.tenant,
            )
        )

        await publisher.publish(
            self._session,
            event_type=f"ticket.{trigger}",
            aggregate_type="ticket",
            aggregate_id=ticket_id,
            payload={"from_state": old_state, "to_state": new_state, "trigger": trigger},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=ticket.customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Ticket {trigger}: {old_state} → {new_state}",
                related_ticket_id=ticket_id,
                related_case_id=ticket.case_id,
                occurred_at=clock_now(),
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return ticket

    async def update_ticket(self, ticket_id: str, **updates: str | None) -> Ticket:
        ticket = await self._ticket_repo.get(ticket_id)
        if not ticket:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="ticket.not_found",
                message=f"Ticket {ticket_id} not found",
                context={"ticket_id": ticket_id},
            )

        if "assigned_to_agent_id" in updates and updates["assigned_to_agent_id"]:
            await ticket_policies.check_agent_active(
                updates["assigned_to_agent_id"], self._customer_repo
            )

        for k, v in updates.items():
            if hasattr(ticket, k) and v is not None:
                setattr(ticket, k, v)

        await self._ticket_repo.update(ticket)
        await self._session.commit()
        return ticket

    async def resolve_ticket(
        self, ticket_id: str, *, resolution_notes: str
    ) -> Ticket:
        return await self.transition_ticket(
            ticket_id, "resolve", resolution_notes=resolution_notes
        )

    async def cancel_ticket(self, ticket_id: str) -> Ticket:
        return await self.transition_ticket(ticket_id, "cancel")
