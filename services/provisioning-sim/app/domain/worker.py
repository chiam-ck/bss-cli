"""Provisioning simulator worker — the heart of the service.

Processes provisioning tasks with configurable fault injection:
- fail_always: every attempt fails (up to max_attempts)
- fail_first_attempt: first attempt fails, subsequent succeed
- slow: simulates network element latency (2x-5x normal duration)
- stuck: task enters stuck state requiring manual intervention
"""

import asyncio
import json
import random
from uuid import uuid4

import aio_pika
import structlog
from bss_clock import now as clock_now
from bss_models.audit import DomainEvent
from bss_models.provisioning import ProvisioningTask
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.domain.esim_provider import EsimProviderAdapter
from app.repositories.fault_repo import FaultRepository
from app.repositories.task_repo import TaskRepository

log = structlog.get_logger()

TASK_DURATIONS = {
    "HLR_PROVISION": 0.5,
    "PCRF_POLICY_PUSH": 0.3,
    "OCS_BALANCE_INIT": 0.2,
    "ESIM_PROFILE_PREPARE": 0.4,
    "HLR_DEPROVISION": 0.4,
}


async def process_task(
    *,
    service_id: str,
    service_order_id: str,
    commercial_order_id: str,
    task_type: str,
    payload: dict,
    session: AsyncSession,
    task_repo: TaskRepository,
    fault_repo: FaultRepository,
    exchange: aio_pika.abc.AbstractExchange | None = None,
    esim_provider: EsimProviderAdapter | None = None,
) -> None:
    """Core worker — process a single provisioning task with fault injection."""
    task_id = await task_repo.next_id()
    task = ProvisioningTask(
        id=task_id,
        service_id=service_id,
        task_type=task_type,
        state="pending",
        attempts=0,
        max_attempts=3,
        payload=payload or {},
    )
    await task_repo.create(task)
    await session.flush()

    # Check fault injection
    fault = await fault_repo.get_active_fault(task_type)

    # Stuck fault — never auto-retry
    if fault and fault.fault_type == "stuck" and _should_fire(fault.probability):
        task.state = "stuck"
        task.started_at = clock_now()
        await _audit_and_publish(
            session, exchange, "provisioning.task.stuck", task,
            service_order_id, commercial_order_id,
        )
        await session.commit()
        log.info("task.stuck", task_id=task_id, task_type=task_type)
        return

    # Process with retry loop
    while task.attempts < task.max_attempts:
        task.attempts += 1
        task.state = "running"
        task.started_at = task.started_at or clock_now()
        await session.flush()

        # fail_always
        if fault and fault.fault_type == "fail_always" and _should_fire(fault.probability):
            task.last_error = f"Simulated fail_always for {task_type}"
            if task.attempts >= task.max_attempts:
                task.state = "failed"
                await _audit_and_publish(
                    session, exchange, "provisioning.task.failed", task,
                    service_order_id, commercial_order_id, permanent=True,
                )
                await session.commit()
                log.info("task.failed.permanent", task_id=task_id, attempts=task.attempts)
                return
            task.state = "failed"
            await session.flush()
            log.info("task.failed.retrying", task_id=task_id, attempts=task.attempts)
            continue

        # fail_first_attempt
        if (
            fault
            and fault.fault_type == "fail_first_attempt"
            and task.attempts == 1
            and _should_fire(fault.probability)
        ):
            task.state = "failed"
            task.last_error = f"Simulated fail_first_attempt for {task_type}"
            await session.flush()
            log.info("task.failed.first_attempt", task_id=task_id)
            continue

        # Simulate work
        duration = TASK_DURATIONS.get(task_type, 0.5)
        if fault and fault.fault_type == "slow" and _should_fire(fault.probability):
            duration *= random.uniform(2.0, 5.0)

        await asyncio.sleep(duration)

        # eSIM SM-DP+ seam (v0.15) — sim provider returns success without
        # additional latency, preserving v0.13/v0.14 timing exactly. Real
        # providers (v0.16+) replace the synthetic sleep above with their
        # own HTTP latency.
        if task_type == "ESIM_PROFILE_PREPARE" and esim_provider is not None:
            esim_result = await esim_provider.order_profile(
                iccid=payload.get("iccid", ""),
                imsi=payload.get("imsi", ""),
                msisdn=payload.get("msisdn", ""),
            )
            if not esim_result.success:
                task.state = "failed"
                task.last_error = (
                    f"SM-DP+ provider declined profile order: {esim_result.provider_reference or 'no reference'}"
                )
                if task.attempts >= task.max_attempts:
                    await _audit_and_publish(
                        session, exchange, "provisioning.task.failed", task,
                        service_order_id, commercial_order_id, permanent=True,
                    )
                    await session.commit()
                    return
                await session.flush()
                continue

        # Success
        task.state = "completed"
        task.completed_at = clock_now()
        await _audit_and_publish(
            session, exchange, "provisioning.task.completed", task,
            service_order_id, commercial_order_id,
        )
        await session.commit()
        log.info("task.completed", task_id=task_id, task_type=task_type, attempts=task.attempts)
        return

    # Safety: should not reach here
    task.state = "failed"
    task.last_error = "max_attempts exhausted"
    await _audit_and_publish(
        session, exchange, "provisioning.task.failed", task,
        service_order_id, commercial_order_id, permanent=True,
    )
    await session.commit()


def _should_fire(probability: float) -> bool:
    return random.random() < probability


async def _audit_and_publish(
    session: AsyncSession,
    exchange: aio_pika.abc.AbstractExchange | None,
    event_type: str,
    task: ProvisioningTask,
    service_order_id: str,
    commercial_order_id: str,
    *,
    permanent: bool = False,
) -> None:
    ctx = auth_context.current()
    payload = {
        "taskId": task.id,
        "serviceId": task.service_id,
        "serviceOrderId": service_order_id,
        "commercialOrderId": commercial_order_id,
        "taskType": task.task_type,
        "attempts": task.attempts,
        "maxAttempts": task.max_attempts,
    }
    if task.state == "completed":
        payload["completedAt"] = task.completed_at.isoformat() if task.completed_at else None
    if task.state == "failed":
        payload["lastError"] = task.last_error or ""
        payload["permanent"] = permanent
    if task.state == "stuck":
        payload["startedAt"] = task.started_at.isoformat() if task.started_at else None

    event = DomainEvent(
        event_id=uuid4(),
        event_type=event_type,
        aggregate_type="provisioning_task",
        aggregate_id=task.id,
        occurred_at=clock_now(),
        actor=ctx.actor,
        channel=ctx.channel,
        tenant_id=ctx.tenant,
        payload=payload,
        schema_version=1,
        published_to_mq=False,
    )
    session.add(event)

    if exchange:
        try:
            msg = aio_pika.Message(
                body=json.dumps(payload).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await exchange.publish(msg, routing_key=event_type)
            event.published_to_mq = True
        except Exception:
            log.warning("mq.publish.failed", event_type=event_type, task_id=task.id)
