"""Provisioning task Pydantic schemas — camelCase for API."""

from datetime import datetime

from bss_models.provisioning import FaultInjection, ProvisioningTask
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

TASK_PATH = "/provisioning-api/v1/task"


class TaskResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    href: str
    service_id: str
    task_type: str
    state: str
    attempts: int
    max_attempts: int
    payload: dict | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    at_type: str = Field(default="ProvisioningTask", serialization_alias="@type")


class FaultInjectionResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    task_type: str
    fault_type: str
    probability: float
    enabled: bool
    at_type: str = Field(default="FaultInjection", serialization_alias="@type")


class FaultInjectionUpdateRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool | None = None
    probability: float | None = None
    fault_type: str | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    note: str


def to_task_response(task: ProvisioningTask) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        href=f"{TASK_PATH}/{task.id}",
        service_id=task.service_id,
        task_type=task.task_type,
        state=task.state,
        attempts=task.attempts,
        max_attempts=task.max_attempts,
        payload=task.payload,
        last_error=task.last_error,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


def to_fault_injection_response(fault: FaultInjection) -> FaultInjectionResponse:
    return FaultInjectionResponse(
        id=fault.id,
        task_type=fault.task_type,
        fault_type=fault.fault_type,
        probability=float(fault.probability),
        enabled=fault.enabled,
    )
