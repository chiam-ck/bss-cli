"""Provisioning task policies — invariant enforcement."""

from app import auth_context
from app.policies.base import PolicyViolation, policy


@policy("provisioning_task.retry.max_attempts")
def check_retry_allowed(attempts: int, max_attempts: int, task_id: str) -> None:
    """Task must not have exhausted its retry budget."""
    if attempts >= max_attempts:
        raise PolicyViolation(
            rule="provisioning_task.retry.max_attempts",
            message=f"Task {task_id} has exhausted all {max_attempts} attempts",
            context={
                "task_id": task_id,
                "attempts": attempts,
                "max_attempts": max_attempts,
            },
        )


@policy("provisioning.resolve_stuck.requires_note")
def check_resolve_has_note(note: str, task_id: str) -> None:
    """Resolving a stuck task requires an operator note."""
    if not note or not note.strip():
        raise PolicyViolation(
            rule="provisioning.resolve_stuck.requires_note",
            message=f"Resolving stuck task {task_id} requires a non-empty note",
            context={"task_id": task_id},
        )


@policy("provisioning.set_fault_injection.admin_only")
def check_fault_injection_permission() -> None:
    """Fault injection changes require admin permission (stub: always passes in v0.1)."""
    # v0.1: auth_context always has '*' permissions, so this always passes.
    # Phase 12 will enforce 'provisioning.fault_injection.manage' permission.
    if not auth_context.has_permission("provisioning.fault_injection.manage"):
        raise PolicyViolation(
            rule="provisioning.set_fault_injection.admin_only",
            message="Fault injection management requires admin permission",
            context={},
        )
