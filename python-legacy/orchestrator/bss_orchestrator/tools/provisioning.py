"""Provisioning-sim tools — tasks + fault injection."""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    FaultType,
    ProvisioningTaskId,
    ProvisioningTaskState,
    ProvisioningTaskType,
    ServiceId,
)
from ._registry import register


@register("provisioning.list_tasks")
async def provisioning_list_tasks(
    service_id: ServiceId | None = None,
    state: ProvisioningTaskState | None = None,
) -> list[dict[str, Any]]:
    """List provisioning tasks. Use ``state="stuck"`` to find tasks that need
    manual intervention; use ``service_id`` when diagnosing a specific service.

    Args:
        service_id: Optional filter by owning service.
        state: Optional filter by task state.

    Returns:
        List of task dicts ``{id, serviceId, taskType, state, attempts, maxAttempts}``.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().provisioning.list_tasks(service_id=service_id, state=state)


@register("provisioning.get_task")
async def provisioning_get_task(task_id: ProvisioningTaskId) -> dict[str, Any]:
    """Read a single provisioning task with error + timing detail.

    Args:
        task_id: Provisioning Task ID with the PTK- prefix (opaque suffix).

    Returns:
        Task dict including ``lastError``, ``startedAt``, ``completedAt``.

    Raises:
        NotFound: unknown task.
    """
    return await get_clients().provisioning.get_task(task_id)


@register("provisioning.resolve_stuck")
async def provisioning_resolve_stuck(
    task_id: ProvisioningTaskId,
    note: str,
) -> dict[str, Any]:
    """Manually resolve a STUCK provisioning task (e.g. HLR op completed
    out-of-band by network ops). Only valid when the task is in ``stuck`` state.

    Args:
        task_id: Provisioning Task ID with the PTK- prefix (opaque suffix).
        note: Required free-text note describing what was done.

    Returns:
        Updated task dict with ``state="completed"``.

    Raises:
        PolicyViolationFromServer:
            - ``provisioning.resolve_stuck.task_not_stuck``: the task is in
              a different state; use ``provisioning.retry_failed`` for
              ``failed`` tasks.
    """
    return await get_clients().provisioning.resolve_task(task_id, note=note)


@register("provisioning.retry_failed")
async def provisioning_retry_failed(task_id: ProvisioningTaskId) -> dict[str, Any]:
    """Retry a FAILED provisioning task (up to ``maxAttempts``).

    Args:
        task_id: Provisioning Task ID with the PTK- prefix (opaque suffix).

    Returns:
        Updated task dict.

    Raises:
        PolicyViolationFromServer:
            - ``provisioning.retry.max_attempts_reached``: human intervention
              required; open a ticket.
    """
    return await get_clients().provisioning.retry_task(task_id)


@register("provisioning.set_fault_injection")
async def provisioning_set_fault_injection(
    task_type: ProvisioningTaskType,
    fault_type: FaultType,
    enabled: bool,
    probability: float | None = None,
) -> dict[str, Any]:
    """Toggle / adjust fault injection for a provisioning task type. Admin /
    scenario use — DESTRUCTIVE by policy. First reads the configured
    injector for ``(task_type, fault_type)``, then patches it.

    Args:
        task_type: Task type, e.g. ``HLR_PROVISION``.
        fault_type: One of ``fail_first_attempt``, ``fail_always``, ``stuck``, ``slow``.
        enabled: Whether the injector fires.
        probability: Optional probability in [0.0, 1.0].

    Returns:
        Updated fault-injection dict.

    Raises:
        NotFound: no injector configured for this (task_type, fault_type) pair.
    """
    c = get_clients()
    injectors = await c.provisioning.list_fault_injection()
    target = next(
        (i for i in injectors if i["taskType"] == task_type and i["faultType"] == fault_type),
        None,
    )
    if target is None:
        return {
            "error": "NOT_FOUND",
            "message": f"No fault-injection configured for {task_type}/{fault_type}.",
        }
    return await c.provisioning.update_fault_injection(
        target["id"], enabled=enabled, probability=probability
    )
