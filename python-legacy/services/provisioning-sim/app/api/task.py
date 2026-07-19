"""Provisioning task API routers — no business logic."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_provisioning_service
from app.schemas.task import (
    ResolveRequest,
    TaskResponse,
    to_task_response,
)
from app.services.provisioning_service import ProvisioningService

router = APIRouter(tags=["Provisioning Task"])


@router.get("/task/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    svc: ProvisioningService = Depends(get_provisioning_service),
) -> TaskResponse:
    task = await svc.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return to_task_response(task)


@router.get("/task", response_model=list[TaskResponse])
async def list_tasks(
    serviceId: str | None = None,
    state: str | None = None,
    svc: ProvisioningService = Depends(get_provisioning_service),
) -> list[TaskResponse]:
    tasks = await svc.list_tasks(service_id=serviceId, state=state)
    return [to_task_response(t) for t in tasks]


@router.post("/task/{task_id}/resolve", response_model=TaskResponse)
async def resolve_stuck(
    task_id: str,
    body: ResolveRequest,
    svc: ProvisioningService = Depends(get_provisioning_service),
) -> TaskResponse:
    task = await svc.resolve_stuck(task_id, body.note)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found after resolve")
    return to_task_response(task)


@router.post("/task/{task_id}/retry", response_model=TaskResponse)
async def retry_failed(
    task_id: str,
    svc: ProvisioningService = Depends(get_provisioning_service),
) -> TaskResponse:
    task = await svc.retry_failed(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found after retry")
    return to_task_response(task)
