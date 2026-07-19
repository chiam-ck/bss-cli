"""Agent DTOs."""

from pydantic import BaseModel


class AgentResponse(BaseModel):
    id: str
    name: str
    email: str | None = None
    role: str | None = None
    status: str


def to_agent_response(a) -> AgentResponse:
    return AgentResponse(
        id=a.id,
        name=a.name,
        email=a.email,
        role=a.role,
        status=a.status,
    )
