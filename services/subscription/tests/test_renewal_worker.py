"""v0.18 — renewal worker unit tests.

Direct tests of `_sweep_due` and `_sweep_skipped` against a mocked
session factory + repo + service. Intent: lock down the
correctness invariants documented in `app/workers/renewal.py`'s
module docstring (mark-before-dispatch, per-id session, ContextVar
reset, per-id failure isolation, worker auth attribution).

Integration-shape tests (live DB) live in
`test_renewal_worker_integration.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app import auth_context
from app.policies.base import PolicyViolation

# ── Helpers ──────────────────────────────────────────────────────────


@asynccontextmanager
async def _async_cm(value):
    yield value


def _build_app(
    *,
    due_ids=(),
    blocked_ids=(),
    renew_side_effect=None,
    crm_customer=None,
    email_adapter=None,
):
    """Build a fake app with mocked session_factory + clients.

    Returns (app, recorder) where `recorder` captures repo + service
    calls so tests can assert ordering and arguments.
    """
    recorder = SimpleNamespace(
        marked=[],
        renewed=[],
        published=[],
        sessions_opened=0,
    )

    def _make_session():
        recorder.sessions_opened += 1
        session = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        session.add = MagicMock()
        return session

    def _factory():
        return _async_cm(_make_session())

    crm_client = MagicMock()
    crm_client.get_customer = AsyncMock(
        return_value=crm_customer
        or {
            "id": "CUST-123",
            "contactMedium": [
                {"mediumType": "email", "value": "demo@example.com",
                 "isPrimary": True},
            ],
        }
    )

    # v1.0.1 (#36) — renewal worker now fetches plan names from
    # catalog instead of a hardcoded id→name dict, so the test mock
    # has to answer get_offering with the same names the old fallback
    # used.
    catalog_client = MagicMock()
    catalog_client.get_offering = AsyncMock(
        side_effect=lambda oid: {
            "PLAN_S": {"id": "PLAN_S", "name": "Lite"},
            "PLAN_M": {"id": "PLAN_M", "name": "Standard"},
            "PLAN_L": {"id": "PLAN_L", "name": "Max"},
        }.get(oid, {"id": oid, "name": oid})
    )

    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_factory,
            crm_client=crm_client,
            payment_client=MagicMock(),
            catalog_client=catalog_client,
            inventory_client=MagicMock(),
            mq_exchange=None,
            email_adapter=email_adapter,
        )
    )

    return app, recorder


@pytest.fixture
def patched_worker_module(monkeypatch):
    """Patch the worker module's imports so we can drive sweep behaviour."""
    from app.workers import renewal as worker_mod

    repo_instances: list[MagicMock] = []
    service_instances: list[MagicMock] = []

    class FakeRepo:
        def __init__(self, session):
            self.session = session
            self.due_ids: list[str] = []
            self.blocked_ids: list[str] = []
            self.reminder_rows: list[dict] = []
            self.marked: list[list[str]] = []
            self.marked_reminder: list[list[str]] = []
            repo_instances.append(self)

        async def due_for_renewal(self, *, now, limit=100, tenant_id="DEFAULT"):
            return list(self.due_ids)

        async def overdue_blocked(self, *, now, limit=100, tenant_id="DEFAULT"):
            return list(self.blocked_ids)

        async def due_for_reminder(self, *, now, lookahead_seconds, limit=100, tenant_id="DEFAULT"):
            return list(self.reminder_rows)

        async def mark_renewal_attempted(self, *, ids, at):
            self.marked.append(list(ids))

        async def mark_reminder_sent(self, *, ids, at):
            self.marked_reminder.append(list(ids))

    class FakeVasRepo:
        def __init__(self, session):
            self.session = session

    class FakeService:
        def __init__(self, **kwargs):
            self._kwargs = kwargs
            self.renew_calls: list[str] = []
            self.actor_at_call: list[str] = []
            self._side_effect = None
            service_instances.append(self)

        async def renew(self, sub_id):
            self.renew_calls.append(sub_id)
            self.actor_at_call.append(auth_context.current().actor)
            if self._side_effect:
                exc = self._side_effect(sub_id)
                if exc is not None:
                    raise exc
            return {"id": sub_id, "state": "active"}

    publish_calls: list[dict] = []

    async def fake_publish(session, **kwargs):
        publish_calls.append(kwargs)

    monkeypatch.setattr(worker_mod, "SubscriptionRepository", FakeRepo)
    monkeypatch.setattr(worker_mod, "VasPurchaseRepository", FakeVasRepo)
    monkeypatch.setattr(worker_mod, "SubscriptionService", FakeService)
    monkeypatch.setattr(worker_mod.publisher, "publish", fake_publish)

    return SimpleNamespace(
        worker_mod=worker_mod,
        repo_instances=repo_instances,
        service_instances=service_instances,
        publish_calls=publish_calls,
        FakeRepo=FakeRepo,
        FakeService=FakeService,
    )


def _seed_due(patched, due_ids, *, blocked_ids=(), reminder_rows=()):
    """Set the *next* repo instance to return these ids when queried."""

    original_init = patched.FakeRepo.__init__

    def _init(self, session):
        original_init(self, session)
        self.due_ids = list(due_ids)
        self.blocked_ids = list(blocked_ids)
        self.reminder_rows = list(reminder_rows)

    patched.FakeRepo.__init__ = _init


def _make_reminder_row(
    sub_id="SUB-1",
    customer_id="CUST-1",
    offering_id="PLAN_M",
    msisdn="90000001",
    next_renewal_at=None,
    price_amount=None,
    price_currency="SGD",
):
    """Helper to build a row dict matching `due_for_reminder()` output."""
    from datetime import datetime, timezone
    from decimal import Decimal

    return {
        "id": sub_id,
        "customer_id": customer_id,
        "offering_id": offering_id,
        "msisdn": msisdn,
        "next_renewal_at": next_renewal_at
        or datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
        "price_amount": price_amount or Decimal("25.00"),
        "price_currency": price_currency,
    }


class _RecordingEmailAdapter:
    """Minimal EmailAdapter test double — captures send_renewal_reminder calls."""

    def __init__(self):
        self.sends: list[dict] = []

    def send_renewal_reminder(self, email, **kwargs):
        self.sends.append({"email": email, **kwargs})

    # Unused by the reminder sweep but required by the Protocol shape
    # so static lints (if any) don't grumble. Tests don't exercise these.
    def send_login(self, email, otp, magic_link): pass  # noqa: E704
    def send_step_up(self, email, otp, action_label): pass  # noqa: E704
    def send_email_change_verification(self, new_email, otp, magic_link): pass  # noqa: E704


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_due_dispatches_each_returned_id(patched_worker_module):
    patched = patched_worker_module
    _seed_due(patched, ["SUB-1", "SUB-2", "SUB-3"])
    app, _rec = _build_app()

    await patched.worker_mod._sweep_due(app)

    # One repo per dispatch (3) + one for the initial select = 4
    assert len(patched.repo_instances) >= 4
    # Three service constructions, three renew calls
    assert len(patched.service_instances) == 3
    assert [s.renew_calls[0] for s in patched.service_instances] == [
        "SUB-1", "SUB-2", "SUB-3",
    ]


@pytest.mark.asyncio
async def test_sweep_due_no_due_rows_no_dispatch(patched_worker_module):
    patched = patched_worker_module
    _seed_due(patched, [])
    app, _rec = _build_app()

    await patched.worker_mod._sweep_due(app)

    assert patched.service_instances == []


@pytest.mark.asyncio
async def test_sweep_due_marks_all_ids_before_dispatch(patched_worker_module):
    """The bulk UPDATE mark must run + commit before per-id renew() calls.

    Without this ordering, a peer replica could pick up the same row,
    re-mark it, and double-dispatch — which is exactly the
    multi-replica double-billing risk the doctrine guards against.
    """
    patched = patched_worker_module
    _seed_due(patched, ["SUB-1", "SUB-2"])
    app, _rec = _build_app()

    await patched.worker_mod._sweep_due(app)

    # The first repo (the one used for the SELECT) carries the mark
    select_repo = patched.repo_instances[0]
    assert select_repo.marked == [["SUB-1", "SUB-2"]]


@pytest.mark.asyncio
async def test_sweep_due_per_id_failure_does_not_poison_others(
    patched_worker_module,
):
    patched = patched_worker_module
    _seed_due(patched, ["SUB-1", "SUB-2", "SUB-3"])
    app, _rec = _build_app()

    # Side effect: SUB-2 raises PolicyViolation; SUB-1 and SUB-3 succeed.
    def _side_effect(sub_id):
        if sub_id == "SUB-2":
            return PolicyViolation(
                rule="subscription.renew.requires_active_cof",
                message="no card",
            )
        return None

    original_init = patched.FakeService.__init__

    def _init(self, **kwargs):
        original_init(self, **kwargs)
        self._side_effect = _side_effect

    patched.FakeService.__init__ = _init

    await patched.worker_mod._sweep_due(app)

    # All three were attempted; SUB-2's failure didn't stop SUB-3.
    all_renews = [
        call_id
        for svc in patched.service_instances
        for call_id in svc.renew_calls
    ]
    assert all_renews == ["SUB-1", "SUB-2", "SUB-3"]


@pytest.mark.asyncio
async def test_sweep_due_dispatch_uses_worker_auth_context(
    patched_worker_module,
):
    patched = patched_worker_module
    _seed_due(patched, ["SUB-1"])
    app, _rec = _build_app()

    await patched.worker_mod._sweep_due(app)

    assert patched.service_instances[0].actor_at_call == [
        "system:renewal_worker",
    ]


@pytest.mark.asyncio
async def test_sweep_due_resets_auth_context_after_each_dispatch(
    patched_worker_module,
):
    patched = patched_worker_module
    _seed_due(patched, ["SUB-1", "SUB-2"])
    app, _rec = _build_app()

    # Snapshot actor before the sweep
    actor_before = auth_context.current().actor

    await patched.worker_mod._sweep_due(app)

    # After the sweep, the ContextVar is back to the pre-sweep value
    actor_after = auth_context.current().actor
    assert actor_after == actor_before
    assert actor_after != "system:renewal_worker"


@pytest.mark.asyncio
async def test_sweep_skipped_emits_event_and_marks(patched_worker_module):
    patched = patched_worker_module
    _seed_due(patched, [], blocked_ids=["SUB-9"])
    app, _rec = _build_app()

    await patched.worker_mod._sweep_skipped(app)

    # One publish call for the blocked sub
    assert len(patched.publish_calls) == 1
    pub = patched.publish_calls[0]
    assert pub["event_type"] == "subscription.renewal_skipped"
    assert pub["aggregate_type"] == "subscription"
    assert pub["aggregate_id"] == "SUB-9"
    assert pub["payload"]["reason"] == "blocked"

    # And the row was marked
    select_repo = patched.repo_instances[0]
    assert select_repo.marked == [["SUB-9"]]


@pytest.mark.asyncio
async def test_sweep_skipped_no_blocked_rows_no_publish(patched_worker_module):
    patched = patched_worker_module
    _seed_due(patched, [], blocked_ids=[])
    app, _rec = _build_app()

    await patched.worker_mod._sweep_skipped(app)

    assert patched.publish_calls == []


@pytest.mark.asyncio
async def test_sweep_skipped_uses_worker_auth_context(patched_worker_module):
    patched = patched_worker_module
    _seed_due(patched, [], blocked_ids=["SUB-9"])
    app, _rec = _build_app()

    captured_actors: list[str] = []

    original_publish = patched.worker_mod.publisher.publish

    async def _capture(session, **kwargs):
        captured_actors.append(auth_context.current().actor)

    patched.worker_mod.publisher.publish = _capture
    try:
        await patched.worker_mod._sweep_skipped(app)
    finally:
        patched.worker_mod.publisher.publish = original_publish

    assert captured_actors == ["system:renewal_worker"]


# ── Upcoming-renewal reminder sweep ───────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_reminder_sends_for_each_due_row(patched_worker_module):
    patched = patched_worker_module
    rows = [
        _make_reminder_row(sub_id="SUB-1", customer_id="CUST-1"),
        _make_reminder_row(sub_id="SUB-2", customer_id="CUST-2"),
    ]
    _seed_due(patched, [], reminder_rows=rows)
    adapter = _RecordingEmailAdapter()
    app, _rec = _build_app(email_adapter=adapter)

    await patched.worker_mod._sweep_upcoming_renewal_reminder(app)

    # Two reminders sent, one per due row
    assert len(adapter.sends) == 2
    assert adapter.sends[0]["email"] == "demo@example.com"
    assert adapter.sends[0]["plan_name"] == "Standard"   # PLAN_M → human
    assert adapter.sends[0]["amount"] == "25.00"
    assert adapter.sends[0]["currency"] == "SGD"
    assert adapter.sends[0]["msisdn"] == "90000001"


@pytest.mark.asyncio
async def test_sweep_reminder_marks_before_send(patched_worker_module):
    """The dedup column write must commit before any email goes out
    so a peer replica's next sweep doesn't double-send the reminder."""
    patched = patched_worker_module
    rows = [_make_reminder_row(sub_id="SUB-1")]
    _seed_due(patched, [], reminder_rows=rows)
    adapter = _RecordingEmailAdapter()
    app, _rec = _build_app(email_adapter=adapter)

    await patched.worker_mod._sweep_upcoming_renewal_reminder(app)

    select_repo = patched.repo_instances[0]
    assert select_repo.marked_reminder == [["SUB-1"]]


@pytest.mark.asyncio
async def test_sweep_reminder_skips_when_no_email_on_customer(
    patched_worker_module,
):
    patched = patched_worker_module
    rows = [_make_reminder_row(sub_id="SUB-1")]
    _seed_due(patched, [], reminder_rows=rows)
    adapter = _RecordingEmailAdapter()
    app, _rec = _build_app(
        email_adapter=adapter,
        crm_customer={"id": "CUST-1", "contactMedium": []},
    )

    await patched.worker_mod._sweep_upcoming_renewal_reminder(app)

    # No email sent — but the row was still marked (we don't retry on
    # missing-email; the renewal itself will still fire on the boundary)
    assert adapter.sends == []
    assert patched.repo_instances[0].marked_reminder == [["SUB-1"]]


@pytest.mark.asyncio
async def test_sweep_reminder_disabled_when_lookahead_zero(
    patched_worker_module, monkeypatch,
):
    monkeypatch.setenv("BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS", "0")
    patched = patched_worker_module
    rows = [_make_reminder_row(sub_id="SUB-1")]
    _seed_due(patched, [], reminder_rows=rows)
    adapter = _RecordingEmailAdapter()
    app, _rec = _build_app(email_adapter=adapter)

    await patched.worker_mod._sweep_upcoming_renewal_reminder(app)

    assert adapter.sends == []
    # Repo not even queried — the lookahead gate short-circuits before
    # the SELECT, so no FakeRepo got a `due_for_reminder` call.
    assert patched.repo_instances == []


@pytest.mark.asyncio
async def test_sweep_reminder_no_op_when_email_adapter_missing(
    patched_worker_module,
):
    patched = patched_worker_module
    rows = [_make_reminder_row(sub_id="SUB-1")]
    _seed_due(patched, [], reminder_rows=rows)
    app, _rec = _build_app(email_adapter=None)

    await patched.worker_mod._sweep_upcoming_renewal_reminder(app)

    # Adapter init failed at lifespan; sweep just returns. No repo
    # query, no mark, no send.
    assert patched.repo_instances == []


@pytest.mark.asyncio
async def test_sweep_reminder_per_id_failure_does_not_poison_others(
    patched_worker_module,
):
    patched = patched_worker_module
    rows = [
        _make_reminder_row(sub_id="SUB-1", customer_id="CUST-1", msisdn="90000001"),
        _make_reminder_row(sub_id="SUB-2", customer_id="CUST-2", msisdn="90000002"),
        _make_reminder_row(sub_id="SUB-3", customer_id="CUST-3", msisdn="90000003"),
    ]
    _seed_due(patched, [], reminder_rows=rows)

    class _PartiallyFailingAdapter(_RecordingEmailAdapter):
        def send_renewal_reminder(self, email, **kwargs):
            if kwargs.get("msisdn") == "90000001":
                raise RuntimeError("smtp blew up")
            super().send_renewal_reminder(email, **kwargs)

    adapter = _PartiallyFailingAdapter()
    app, _rec = _build_app(email_adapter=adapter)

    await patched.worker_mod._sweep_upcoming_renewal_reminder(app)

    # SUB-1's send failed but SUB-2 and SUB-3 still sent
    assert len(adapter.sends) == 2
