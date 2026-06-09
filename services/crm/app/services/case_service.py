"""Case service — orchestrates policies, repos, events."""

from uuid import uuid4

import structlog
from bss_clock import now as clock_now
from bss_models.crm import Case, CaseNote, Interaction
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.domain import case_state
from app.events import publisher
from app.policies import case as case_policies
from app.repositories.case_repo import CaseRepository
from app.repositories.customer_repo import CustomerRepository
from app.repositories.interaction_repo import InteractionRepository
from app.repositories.ticket_repo import TicketRepository

log = structlog.get_logger()


def _next_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


class CaseService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        case_repo: CaseRepository,
        customer_repo: CustomerRepository,
        ticket_repo: TicketRepository,
        interaction_repo: InteractionRepository,
    ) -> None:
        self._session = session
        self._case_repo = case_repo
        self._customer_repo = customer_repo
        self._ticket_repo = ticket_repo
        self._interaction_repo = interaction_repo

    async def open_case(
        self,
        *,
        customer_id: str,
        subject: str,
        description: str | None = None,
        priority: str = "normal",
        category: str = "general",
        opened_by_agent_id: str | None = None,
        chat_transcript_hash: str | None = None,
    ) -> Case:
        """Open a case for a customer.

        ``chat_transcript_hash`` (v0.12) links the case to a previously-
        stored ``audit.chat_transcript`` row when the case is opened
        from the customer chat surface via ``case.open_for_me``. The
        column is a soft pointer (no FK constraint) — transcripts may
        be archived independently of cases per the retention runbook.
        """
        ctx = auth_context.current()

        await case_policies.check_customer_active(customer_id, self._customer_repo)

        now = clock_now()
        case_id = _next_id("CASE")
        case = Case(
            id=case_id,
            customer_id=customer_id,
            subject=subject,
            description=description,
            state="open",
            priority=priority,
            category=category,
            opened_by_agent_id=opened_by_agent_id,
            opened_at=now,
            tenant_id=ctx.tenant,
            chat_transcript_hash=chat_transcript_hash,
        )
        await self._case_repo.create(case)

        await publisher.publish(
            self._session,
            event_type="case.opened",
            aggregate_type="case",
            aggregate_id=case_id,
            payload={"customer_id": customer_id, "subject": subject},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Case opened: {subject}",
                related_case_id=case_id,
                occurred_at=now,
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return case

    async def get_case(self, case_id: str) -> Case | None:
        return await self._case_repo.get(case_id)

    async def list_cases(
        self,
        *,
        customer_id: str | None = None,
        state: str | None = None,
        assigned_agent_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Case]:
        return await self._case_repo.list_cases(
            customer_id=customer_id,
            state=state,
            assigned_agent_id=assigned_agent_id,
            limit=limit,
            offset=offset,
        )

    async def transition_case(self, case_id: str, trigger: str, **kwargs: str | None) -> Case:
        ctx = auth_context.current()
        case = await self._case_repo.get(case_id)
        if not case:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="case.not_found",
                message=f"Case {case_id} not found",
                context={"case_id": case_id},
            )

        case_policies.check_case_transition(case.state, trigger)

        # Trigger-specific guards
        if trigger == "resolve":
            await case_policies.check_all_tickets_resolved(case_id, self._ticket_repo)
        elif trigger == "close":
            resolution_code = kwargs.get("resolution_code") or case.resolution_code
            case_policies.check_resolution_code(resolution_code)
            case.resolution_code = resolution_code

        # Apply transition
        old_state = case.state
        new_state = case_state.get_next_state(case.state, trigger)
        case.state = new_state

        if new_state == "closed":
            case.closed_at = clock_now()
            # Cancel open tickets on cancel trigger
            if trigger == "cancel":
                open_tickets = await self._ticket_repo.find_open_by_case(case_id)
                for ticket in open_tickets:
                    ticket.state = "cancelled"
                    ticket.closed_at = clock_now()
                    await self._ticket_repo.update(ticket)

        await self._case_repo.update(case)

        await publisher.publish(
            self._session,
            event_type=f"case.{trigger}",
            aggregate_type="case",
            aggregate_id=case_id,
            payload={"from_state": old_state, "to_state": new_state, "trigger": trigger},
        )
        await self._interaction_repo.create(
            Interaction(
                id=_next_id("INT"),
                customer_id=case.customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"Case {trigger}: {old_state} → {new_state}",
                related_case_id=case_id,
                occurred_at=clock_now(),
                tenant_id=ctx.tenant,
            )
        )
        await self._session.commit()
        return case

    async def close_case(self, case_id: str, *, resolution_code: str) -> Case:
        """Close a case, fast-forwarding through ``resolved`` if needed.

        v0.13.1 — operator UX: in the cockpit, the LLM proposes
        ``case.close`` directly. The state machine requires a case to
        be ``resolved`` before it can transition to ``closed``, but
        forcing the operator (or LLM) to run a separate ``case.transition
        → resolve`` first turns a one-turn intent into a two-tool
        round-trip the model often gets wrong. We collapse that here:
        if the case is in a state from which ``resolve`` is legal
        (``open`` / ``in_progress``), run that transition first.
        Tickets-still-open or other policy violations surface
        unchanged.
        """
        case = await self._case_repo.get(case_id)
        if case is None:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="case.not_found",
                message=f"Case {case_id} not found",
                context={"case_id": case_id},
            )
        if case_state.is_valid_transition(case.state, "resolve"):
            await self.transition_case(case_id, "resolve")
        return await self.transition_case(
            case_id, "close", resolution_code=resolution_code
        )

    async def update_case_fields(
        self,
        case_id: str,
        *,
        priority: str | None = None,
        category: str | None = None,
    ) -> Case:
        """Update mutable case fields (priority/category) — no transition.

        v1.6 — first real write path for `case.update_priority`; the
        PATCH endpoint previously only deserialized triggers, so field
        updates were rejected before reaching the service.
        """
        case = await self._case_repo.get(case_id)
        if not case:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="case.not_found",
                message=f"Case {case_id} not found",
                context={"case_id": case_id},
            )
        case_policies.check_case_not_closed(case_id, case.state)
        if priority is not None:
            case_policies.check_priority_valid(priority)

        changed: dict[str, dict[str, str | None]] = {}
        if priority is not None and priority != case.priority:
            changed["priority"] = {"from": case.priority, "to": priority}
            case.priority = priority
        if category is not None and category != case.category:
            changed["category"] = {"from": case.category, "to": category}
            case.category = category

        if changed:
            await self._case_repo.update(case)
            await publisher.publish(
                self._session,
                event_type="case.updated",
                aggregate_type="case",
                aggregate_id=case_id,
                payload={"changed": changed},
            )
            await self._session.commit()
        return case

    async def add_note(
        self, case_id: str, *, body: str, author_agent_id: str | None = None
    ) -> CaseNote:
        ctx = auth_context.current()
        case = await self._case_repo.get(case_id)
        if not case:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="case.not_found",
                message=f"Case {case_id} not found",
                context={"case_id": case_id},
            )
        if case.state == "closed":
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="case.add_note.case_is_closed",
                message=f"Case {case_id} is closed; cannot add notes",
                context={"case_id": case_id, "state": case.state},
            )

        note = CaseNote(
            id=_next_id("NOTE"),
            case_id=case_id,
            author_agent_id=author_agent_id,
            body=body,
            tenant_id=ctx.tenant,
        )
        await self._case_repo.add_note(note)

        await publisher.publish(
            self._session,
            event_type="case.note_added",
            aggregate_type="case",
            aggregate_id=case_id,
            payload={"note_id": note.id},
        )
        await self._session.commit()
        return note
