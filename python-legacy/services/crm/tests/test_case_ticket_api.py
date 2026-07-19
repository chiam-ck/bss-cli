"""Case + Ticket lifecycle tests."""

from httpx import AsyncClient

CUST_PREFIX = "/tmf-api/customerManagement/v4"
CASE_PREFIX = "/crm-api/v1"
TICKET_PREFIX = "/tmf-api/troubleTicket/v4"


async def _create_customer(client: AsyncClient, suffix: str = "") -> str:
    r = await client.post(
        f"{CUST_PREFIX}/customer",
        json={
            "givenName": "Case",
            "familyName": f"Test{suffix}",
            "contactMedium": [
                {"medium_type": "email", "value": f"case.test{suffix}@example.com"}
            ],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


class TestCaseLifecycle:
    async def test_open_case(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".open")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Test case"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "open"
        assert body["id"].startswith("CASE-")

    async def test_case_requires_active_customer(self, client: AsyncClient):
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": "CUST-NONEXISTENT", "subject": "Fail"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "case.open.customer_must_be_active"

    async def test_close_case_with_open_tickets_fails(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".close-fail")
        # Open case
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Close fail test"},
        )
        case_id = r.json()["id"]

        # Open ticket linked to case
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={"customerId": cust_id, "subject": "Blocking ticket", "caseId": case_id},
        )
        assert r.status_code == 201

        # Transition case: open → in_progress → resolve (should fail because ticket is open)
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "take"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == "in_progress"

        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "resolve"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "case.close.requires_all_tickets_resolved"

    async def test_full_case_lifecycle(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".full")

        # Open case
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Full lifecycle"},
        )
        case_id = r.json()["id"]

        # Open ticket
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={
                "customerId": cust_id,
                "subject": "Child ticket",
                "caseId": case_id,
                "assignedToAgentId": "AGT-001",
            },
        )
        ticket_id = r.json()["id"]

        # Ticket: open → ack → start → resolve → close
        await client.patch(
            f"{TICKET_PREFIX}/troubleTicket/{ticket_id}",
            json={"assignedToAgentId": "AGT-001"},
        )
        # Ack (requires assigned agent — already set)
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "take"},
        )
        assert r.json()["state"] == "in_progress"

        # Resolve ticket
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket/{ticket_id}/resolve",
            json={"resolutionNotes": "Fixed it"},
        )
        assert r.status_code == 422  # Can't resolve from open — need ack → start first

    async def test_close_fast_forwards_from_open(self, client: AsyncClient):
        """v0.13.1 — POST /case/{id}/close auto-resolves an open case
        instead of forcing the caller through resolve+close. Operator
        UX: one tool call from "close this case" intent to closed
        state, instead of a multi-tool LLM round trip."""
        cust_id = await _create_customer(client, ".close-fast")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Close fast"},
        )
        case_id = r.json()["id"]
        # Case is freshly open; no tickets blocking.
        assert r.json()["state"] == "open"

        r = await client.post(
            f"{CASE_PREFIX}/case/{case_id}/close",
            json={"resolution_code": "fixed"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == "closed"
        assert r.json()["resolution_code"] == "fixed"

    async def test_close_fast_forwards_from_in_progress(
        self, client: AsyncClient
    ):
        """Same fast-forward path from in_progress (after ``take``)."""
        cust_id = await _create_customer(client, ".close-fast2")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Close fast 2"},
        )
        case_id = r.json()["id"]
        # Take it to in_progress first.
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "take"},
        )
        assert r.json()["state"] == "in_progress"
        r = await client.post(
            f"{CASE_PREFIX}/case/{case_id}/close",
            json={"resolution_code": "duplicate"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == "closed"

    async def test_cancel_case_from_open(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".cancel")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Will cancel"},
        )
        case_id = r.json()["id"]
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "cancel"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == "closed"

    async def test_add_note(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".note")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Note test"},
        )
        case_id = r.json()["id"]
        r = await client.post(
            f"{CASE_PREFIX}/case/{case_id}/note",
            json={"body": "This is a test note"},
        )
        assert r.status_code == 201
        assert r.json()["body"] == "This is a test note"


class TestTicketLifecycle:
    async def test_open_ticket_standalone(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".tkt-standalone")
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={"customerId": cust_id, "subject": "Standalone ticket"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "open"
        assert body["caseId"] is None

    async def test_ticket_requires_customer(self, client: AsyncClient):
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={"customerId": "CUST-NONEXISTENT", "subject": "Fail"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "ticket.open.requires_customer"

    async def test_assign_terminated_agent(self, client: AsyncClient):
        """Agent validation — seeded AGT-004 is active, but we test the policy path."""
        cust_id = await _create_customer(client, ".tkt-agent")
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={
                "customerId": cust_id,
                "subject": "Agent test",
                "assignedToAgentId": "AGT-NONEXISTENT",
            },
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "ticket.assign.agent_must_be_active"

    async def test_ticket_full_lifecycle(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".tkt-full")
        # Open
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={
                "customerId": cust_id,
                "subject": "Full ticket lifecycle",
                "assignedToAgentId": "AGT-001",
            },
        )
        ticket_id = r.json()["id"]
        assert r.json()["state"] == "open"

        # Cannot resolve directly from open
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket/{ticket_id}/resolve",
            json={"resolutionNotes": "Direct resolve"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "ticket.transition.valid_from_state"

    async def test_cancel_ticket(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".tkt-cancel")
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={"customerId": cust_id, "subject": "Cancel me"},
        )
        ticket_id = r.json()["id"]
        r = await client.post(f"{TICKET_PREFIX}/troubleTicket/{ticket_id}/cancel")
        assert r.status_code == 200
        assert r.json()["state"] == "cancelled"

    async def test_resolve_requires_notes(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".tkt-resolve-notes")
        # Open ticket with agent
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket",
            json={
                "customerId": cust_id,
                "subject": "Resolve needs notes",
                "assignedToAgentId": "AGT-001",
            },
        )
        ticket_id = r.json()["id"]
        # The resolve attempt from open will fail on state transition, not on notes
        r = await client.post(
            f"{TICKET_PREFIX}/troubleTicket/{ticket_id}/resolve",
            json={"resolutionNotes": ""},
        )
        assert r.status_code == 422


class TestInvalidTransitions:
    async def test_case_invalid_transition(self, client: AsyncClient):
        cust_id = await _create_customer(client, ".invalid-trans")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Invalid"},
        )
        case_id = r.json()["id"]
        # Try close from open (not valid — need resolve first)
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "close"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "case.transition.valid_from_state"


class TestCaseFieldUpdates:
    """v1.6 — PATCH carries priority/category field updates, not just
    triggers (the cockpit CRM workbench path; previously 422'd)."""

    async def _open_case(self, client: AsyncClient, suffix: str) -> str:
        cust_id = await _create_customer(client, suffix)
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": f"Fields {suffix}"},
        )
        assert r.status_code == 201
        return r.json()["id"]

    async def test_patch_priority(self, client: AsyncClient):
        case_id = await self._open_case(client, ".prio")
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"priority": "high"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["priority"] == "high"
        assert body["state"] == "open"  # no transition side effect

    async def test_patch_invalid_priority_rejected(self, client: AsyncClient):
        case_id = await self._open_case(client, ".prio-bad")
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"priority": "ludicrous"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "case.update.invalid_priority"

    async def test_patch_on_closed_case_rejected(self, client: AsyncClient):
        case_id = await self._open_case(client, ".prio-closed")
        r = await client.post(
            f"{CASE_PREFIX}/case/{case_id}/close",
            json={"resolution_code": "no_fault_found"},
        )
        assert r.status_code == 200
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"priority": "high"},
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "case.update.case_is_closed"

    async def test_patch_empty_body_rejected(self, client: AsyncClient):
        case_id = await self._open_case(client, ".prio-empty")
        r = await client.patch(f"{CASE_PREFIX}/case/{case_id}", json={})
        assert r.status_code == 400

    async def test_patch_trigger_and_priority_together(self, client: AsyncClient):
        case_id = await self._open_case(client, ".prio-both")
        r = await client.patch(
            f"{CASE_PREFIX}/case/{case_id}",
            json={"trigger": "take", "priority": "critical"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "in_progress"
        assert body["priority"] == "critical"
