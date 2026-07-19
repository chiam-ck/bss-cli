"""bss_clock — behaviour tests for the process-local scenario clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from bss_clock import advance, clock_admin_router, freeze, now, parse_duration, state, unfreeze
from bss_clock.clock import reset_for_tests
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_clock():
    reset_for_tests()
    yield
    reset_for_tests()


def test_now_returns_wall_clock_utc_by_default() -> None:
    before = datetime.now(timezone.utc)
    t = now()
    after = datetime.now(timezone.utc)
    assert before <= t <= after
    assert t.tzinfo is timezone.utc


def test_freeze_pins_now_to_provided_instant() -> None:
    target = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    freeze(target)
    assert now() == target
    # Calling twice with different instants shifts to the new one.
    freeze(target + timedelta(hours=1))
    assert now() == target + timedelta(hours=1)


def test_freeze_without_arg_pins_to_current_wall_now() -> None:
    frozen = freeze()
    # Any subsequent ``now()`` returns the same instant — no ticking.
    assert now() == frozen
    assert now() == frozen


def test_advance_on_frozen_clock_shifts_frozen_instant() -> None:
    freeze(datetime(2026, 1, 1, tzinfo=timezone.utc))
    advance(timedelta(days=30))
    assert now() == datetime(2026, 1, 31, tzinfo=timezone.utc)


def test_advance_on_unfrozen_clock_adds_offset_and_keeps_ticking() -> None:
    t0 = now()
    advance("1h")
    t1 = now()
    # Should be roughly 1 hour ahead of t0 (give or take a few ms).
    assert timedelta(minutes=59) < t1 - t0 < timedelta(minutes=61)


def test_unfreeze_drops_back_into_wall_clock() -> None:
    freeze(datetime(2026, 6, 1, tzinfo=timezone.utc))
    unfreeze()
    t = now()
    # Should be close to real wall clock, not the frozen value.
    assert abs((t - datetime.now(timezone.utc)).total_seconds()) < 2


def test_advance_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        advance(timedelta(seconds=-1))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("45s", timedelta(seconds=45)),
        ("15m", timedelta(minutes=15)),
        ("2h", timedelta(hours=2)),
        ("30d", timedelta(days=30)),
        ("  30d  ", timedelta(days=30)),
    ],
)
def test_parse_duration_handles_common_forms(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "30", "30x", "1w", "1h30m", "1.5h"])
def test_parse_duration_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_state_snapshot_reflects_freeze_and_offset() -> None:
    advance("10s")
    s = state()
    assert s.mode == "wall"
    assert s.offset_seconds == 10.0
    assert s.frozen_at is None

    freeze(datetime(2026, 1, 1, tzinfo=timezone.utc))
    s2 = state()
    assert s2.mode == "frozen"
    assert s2.frozen_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


# ─── Admin router ──────────────────────────────────────────────────────────


def _mk_app():
    app = FastAPI()
    app.include_router(clock_admin_router(), prefix="/admin-api/v1")
    return app


def test_get_clock_now_is_unguarded(monkeypatch) -> None:
    monkeypatch.delenv("BSS_ALLOW_ADMIN_RESET", raising=False)
    client = TestClient(_mk_app())
    r = client.get("/admin-api/v1/clock/now")
    assert r.status_code == 200
    assert r.json()["mode"] == "wall"


def test_mutating_endpoints_403_without_flag(monkeypatch) -> None:
    monkeypatch.delenv("BSS_ALLOW_ADMIN_RESET", raising=False)
    client = TestClient(_mk_app())
    for path, body in [
        ("/admin-api/v1/clock/freeze", {}),
        ("/admin-api/v1/clock/unfreeze", {}),
        ("/admin-api/v1/clock/advance", {"duration": "1h"}),
    ]:
        r = client.post(path, json=body)
        assert r.status_code == 403, path


def test_freeze_then_advance_via_http(monkeypatch) -> None:
    monkeypatch.setenv("BSS_ALLOW_ADMIN_RESET", "true")
    client = TestClient(_mk_app())
    r = client.post(
        "/admin-api/v1/clock/freeze",
        json={"at": "2026-06-01T12:00:00+00:00"},
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "frozen"
    assert r.json()["frozenAt"] == "2026-06-01T12:00:00+00:00"

    r = client.post("/admin-api/v1/clock/advance", json={"duration": "2h"})
    assert r.status_code == 200
    assert r.json()["frozenAt"] == "2026-06-01T14:00:00+00:00"


def test_advance_rejects_missing_or_bad_duration(monkeypatch) -> None:
    monkeypatch.setenv("BSS_ALLOW_ADMIN_RESET", "true")
    client = TestClient(_mk_app())
    r = client.post("/admin-api/v1/clock/advance", json={})
    assert r.status_code == 422
    r = client.post("/admin-api/v1/clock/advance", json={"duration": "bogus"})
    assert r.status_code == 422


def test_freeze_rejects_bad_iso(monkeypatch) -> None:
    monkeypatch.setenv("BSS_ALLOW_ADMIN_RESET", "true")
    client = TestClient(_mk_app())
    r = client.post("/admin-api/v1/clock/freeze", json={"at": "not-a-date"})
    assert r.status_code == 422
