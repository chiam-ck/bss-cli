"""Agent read-only endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_customer_repo
from app.repositories.customer_repo import CustomerRepository
from app.schemas.internal.agent import AgentResponse, to_agent_response

router = APIRouter(tags=["Agent"])


@router.get("/agent", response_model=list[AgentResponse])
async def list_agents(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    repo: CustomerRepository = Depends(get_customer_repo),
) -> list[AgentResponse]:
    agents = await repo.list_agents(status=status, limit=limit, offset=offset)
    return [to_agent_response(a) for a in agents]


@router.get("/agent/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    repo: CustomerRepository = Depends(get_customer_repo),
) -> AgentResponse:
    agent = await repo.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return to_agent_response(agent)
