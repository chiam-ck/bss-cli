"""Ticket policies."""

from app.domain import ticket_state
from app.policies.base import PolicyViolation, policy
from app.repositories.customer_repo import CustomerRepository


@policy("ticket.open.requires_customer")
async def check_customer_exists(
    customer_id: str, repo: CustomerRepository
) -> None:
    cust = await repo.get(customer_id)
    if not cust:
        raise PolicyViolation(
            rule="ticket.open.requires_customer",
            message=f"Customer {customer_id} does not exist",
            context={"customer_id": customer_id},
        )


@policy("ticket.assign.agent_must_be_active")
async def check_agent_active(
    agent_id: str, repo: CustomerRepository
) -> None:
    agent = await repo.get_agent(agent_id)
    if not agent:
        raise PolicyViolation(
            rule="ticket.assign.agent_must_be_active",
            message=f"Agent {agent_id} does not exist",
            context={"agent_id": agent_id},
        )
    if agent.status != "active":
        raise PolicyViolation(
            rule="ticket.assign.agent_must_be_active",
            message=f"Agent {agent_id} is not active (status={agent.status})",
            context={"agent_id": agent_id, "status": agent.status},
        )


@policy("ticket.transition.valid_from_state")
def check_ticket_transition(current_state: str, trigger: str) -> None:
    if not ticket_state.is_valid_transition(current_state, trigger):
        raise PolicyViolation(
            rule="ticket.transition.valid_from_state",
            message=f"Cannot '{trigger}' ticket from state '{current_state}'",
            context={"current_state": current_state, "trigger": trigger},
        )


@policy("ticket.resolve.requires_resolution_notes")
def check_resolution_notes(resolution_notes: str | None) -> None:
    if not resolution_notes:
        raise PolicyViolation(
            rule="ticket.resolve.requires_resolution_notes",
            message="Resolution notes are required to resolve a ticket",
            context={},
        )


@policy("ticket.cancel.not_if_resolved_or_closed")
def check_cancel_allowed(current_state: str) -> None:
    if current_state not in ticket_state.CANCELLABLE:
        raise PolicyViolation(
            rule="ticket.cancel.not_if_resolved_or_closed",
            message=f"Cannot cancel ticket in state '{current_state}'",
            context={"current_state": current_state},
        )
