"""Agent read-only endpoint tests."""

from httpx import AsyncClient

PREFIX = "/crm-api/v1"


class TestAgent:
    async def test_list_agents(self, client: AsyncClient):
        r = await client.get(f"{PREFIX}/agent")
        assert r.status_code == 200
        agents = r.json()
        # Seeded agents: AGT-001 through AGT-SYS (5 total)
        assert len(agents) >= 5
        names = {a["name"] for a in agents}
        assert "Alice Tan" in names
        assert "System" in names

    async def test_get_agent(self, client: AsyncClient):
        r = await client.get(f"{PREFIX}/agent/AGT-001")
        assert r.status_code == 200
        assert r.json()["name"] == "Alice Tan"
        assert r.json()["role"] == "csr"

    async def test_get_agent_not_found(self, client: AsyncClient):
        r = await client.get(f"{PREFIX}/agent/AGT-NONEXISTENT")
        assert r.status_code == 404
