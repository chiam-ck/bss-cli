"""TMF683 Customer Interaction endpoints."""


from bss_clock import now as clock_now
from bss_models.crm import Interaction
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.dependencies import get_interaction_repo, get_session
from app.repositories.interaction_repo import InteractionRepository
from app.schemas.tmf.interaction import (
    CreateInteractionRequest,
    Tmf683Interaction,
    to_tmf683_interaction,
)

router = APIRouter(tags=["TMF683 Interaction"])

from uuid import uuid4


def _next_id() -> str:
    return f"INT-{uuid4().hex[:8]}"


@router.post("/interaction", response_model=Tmf683Interaction, response_model_by_alias=True, status_code=201)
async def create_interaction(
    body: CreateInteractionRequest,
    session: AsyncSession = Depends(get_session),
    repo: InteractionRepository = Depends(get_interaction_repo),
) -> Tmf683Interaction:
    ctx = auth_context.current()
    interaction = Interaction(
        id=_next_id(),
        customer_id=body.customer_id,
        channel=body.channel or ctx.channel,
        direction=body.direction,
        summary=body.summary,
        body=body.body,
        agent_id=body.agent_id,
        related_case_id=body.related_case_id,
        related_ticket_id=body.related_ticket_id,
        occurred_at=clock_now(),
        tenant_id=ctx.tenant,
    )
    await repo.create(interaction)
    await session.commit()
    return to_tmf683_interaction(interaction)


@router.get("/interaction", response_model=list[Tmf683Interaction], response_model_by_alias=True)
async def list_interactions(
    customerId: str,
    limit: int = 50,
    offset: int = 0,
    repo: InteractionRepository = Depends(get_interaction_repo),
) -> list[Tmf683Interaction]:
    interactions = await repo.list_for_customer(customerId, limit=limit, offset=offset)
    return [to_tmf683_interaction(i) for i in interactions]
