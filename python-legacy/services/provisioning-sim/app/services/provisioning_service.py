"""Provisioning service — orchestration layer.

Router -> Service -> Policies -> Repository -> Event publisher.
"""

import aio_pika
import structlog
from bss_models.provisioning import FaultInjection, ProvisioningTask
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.esim_provider import EsimProviderAdapter
from app.domain.worker import process_task
from app.policies.base import PolicyViolation
from app.policies.task import (
    check_fault_injection_permission,
    check_resolve_has_note,
    check_retry_allowed,
)
from app.repositories.fault_repo import FaultRepository
from app.repositories.task_repo import TaskRepository

log = structlog.get_logger()


class ProvisioningService:
    def __init__(
        self,
        session: AsyncSession,
        task_repo: TaskRepository,
        fault_repo: FaultRepository,
        exchange: aio_pika.abc.AbstractExchange | None = None,
        esim_provider: EsimProviderAdapter | None = None,
    ):
        self._session = session
        self._task_repo = task_repo
        self._fault_repo = fault_repo
        self._exchange = exchange
        self._esim_provider = esim_provider

    async def get_task(self, task_id: str) -> ProvisioningTask | None:
        return await self._task_repo.get(task_id)

    async def list_tasks(
        self,
        service_id: str | None = None,
        state: str | None = None,
    ) -> list[ProvisioningTask]:
        return await self._task_repo.list_tasks(service_id=service_id, state=state)

    async def resolve_stuck(self, task_id: str, note: str) -> ProvisioningTask:
        """Resolve a stuck task — policy check, transition stuck->pending, re-process."""
        # Policy: note is required
        check_resolve_has_note(note, task_id)

        task = await self._task_repo.get(task_id)
        if not task:
            raise PolicyViolation(
                rule="provisioning_task.not_found",
                message=f"Task {task_id} not found",
                context={"task_id": task_id},
            )

        if task.state != "stuck":
            raise PolicyViolation(
                rule="provisioning_task.resolve.requires_stuck_state",
                message=f"Task {task_id} is in state '{task.state}', not 'stuck'",
                context={"task_id": task_id, "state": task.state},
            )

        # Reset and re-process
        task.state = "pending"
        task.attempts = 0
        task.last_error = f"Resolved by operator: {note}"
        await self._task_repo.update(task)

        # Re-process the task
        # We need the original service_order_id and commercial_order_id from the payload
        payload = task.payload or {}
        await process_task(
            service_id=task.service_id,
            service_order_id=payload.get("serviceOrderId", ""),
            commercial_order_id=payload.get("commercialOrderId", ""),
            task_type=task.task_type,
            payload=payload,
            session=self._session,
            task_repo=self._task_repo,
            fault_repo=self._fault_repo,
            exchange=self._exchange,
            esim_provider=self._esim_provider,
        )

        # Return updated task
        return await self._task_repo.get(task_id)

    async def retry_failed(self, task_id: str) -> ProvisioningTask:
        """Retry a failed task — policy check, re-process."""
        task = await self._task_repo.get(task_id)
        if not task:
            raise PolicyViolation(
                rule="provisioning_task.not_found",
                message=f"Task {task_id} not found",
                context={"task_id": task_id},
            )

        if task.state != "failed":
            raise PolicyViolation(
                rule="provisioning_task.retry.requires_failed_state",
                message=f"Task {task_id} is in state '{task.state}', not 'failed'",
                context={"task_id": task_id, "state": task.state},
            )

        # Policy: check retry budget
        check_retry_allowed(task.attempts, task.max_attempts, task_id)

        # Reset and re-process
        task.state = "pending"
        task.attempts = 0
        task.last_error = None
        await self._task_repo.update(task)

        payload = task.payload or {}
        await process_task(
            service_id=task.service_id,
            service_order_id=payload.get("serviceOrderId", ""),
            commercial_order_id=payload.get("commercialOrderId", ""),
            task_type=task.task_type,
            payload=payload,
            session=self._session,
            task_repo=self._task_repo,
            fault_repo=self._fault_repo,
            exchange=self._exchange,
            esim_provider=self._esim_provider,
        )

        return await self._task_repo.get(task_id)

    async def list_fault_rules(self) -> list[FaultInjection]:
        return await self._fault_repo.list_all()

    async def update_fault_rule(
        self,
        fault_id: str,
        *,
        enabled: bool | None = None,
        probability: float | None = None,
        fault_type: str | None = None,
    ) -> FaultInjection:
        # Policy: admin only
        check_fault_injection_permission()

        fault = await self._fault_repo.get(fault_id)
        if not fault:
            raise PolicyViolation(
                rule="provisioning.fault_injection.not_found",
                message=f"Fault injection rule {fault_id} not found",
                context={"fault_id": fault_id},
            )

        if enabled is not None:
            fault.enabled = enabled
        if probability is not None:
            fault.probability = probability
        if fault_type is not None:
            fault.fault_type = fault_type

        await self._fault_repo.update(fault)
        await self._session.commit()
        log.info(
            "fault_injection.updated",
            fault_id=fault_id,
            enabled=fault.enabled,
            probability=float(fault.probability),
            fault_type=fault.fault_type,
        )
        return fault
