"""Provisioning task API tests."""

from datetime import datetime, timezone

from bss_models.provisioning import ProvisioningTask


async def _seed_task(client, task_id="PTK-9001", state="completed"):
    """Insert a task directly via the test session."""
    app = client._transport.app
    async with app.state.session_factory() as session:
        task = ProvisioningTask(
            id=task_id,
            service_id="SVC-0001",
            task_type="HLR_PROVISION",
            state=state,
            attempts=1,
            max_attempts=3,
            payload={"msisdn": "90000001"},
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc) if state == "completed" else None,
        )
        session.add(task)
        await session.flush()


async def test_get_task(client):
    await _seed_task(client, "PTK-9001", "completed")
    resp = await client.get("/provisioning-api/v1/task/PTK-9001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "PTK-9001"
    assert body["taskType"] == "HLR_PROVISION"
    assert body["state"] == "completed"
    assert body["@type"] == "ProvisioningTask"


async def test_get_task_not_found(client):
    resp = await client.get("/provisioning-api/v1/task/PTK-9999")
    assert resp.status_code == 404


async def test_list_tasks(client):
    await _seed_task(client, "PTK-9002", "completed")
    resp = await client.get("/provisioning-api/v1/task")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1


async def test_list_tasks_filter_by_state(client):
    await _seed_task(client, "PTK-9003", "stuck")
    resp = await client.get("/provisioning-api/v1/task", params={"state": "stuck"})
    assert resp.status_code == 200
    body = resp.json()
    assert all(t["state"] == "stuck" for t in body)


async def test_resolve_stuck_requires_note(client):
    await _seed_task(client, "PTK-9004", "stuck")
    resp = await client.post(
        "/provisioning-api/v1/task/PTK-9004/resolve",
        json={"note": ""},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "provisioning.resolve_stuck.requires_note"


async def test_retry_requires_failed_state(client):
    await _seed_task(client, "PTK-9005", "completed")
    resp = await client.post("/provisioning-api/v1/task/PTK-9005/retry")
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "provisioning_task.retry.requires_failed_state"
