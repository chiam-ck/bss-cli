"""Unit tests for soak runner pieces that don't need the live stack.

The actual soak quality is established by running it (PR11). Here we
just confirm the report renderer + corpus invariants hold so a typo
in either fails before the 70-minute investment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import get_args

from scenarios.soak.corpus import (
    CROSS_CUSTOMER_PROBES,
    ESCALATION_TRIGGERS,
    NORMAL_ASKS,
    all_asks,
)
from scenarios.soak.metrics import (
    DBSnapshot,
    SoakRunMetrics,
    render_report,
)
from scenarios.soak.synthetic_customer import TurnResult

# ─── Corpus invariants ──────────────────────────────────────────────


def test_normal_asks_non_empty() -> None:
    assert len(NORMAL_ASKS) >= 30
    # Every entry is non-blank.
    assert all(s.strip() for s in NORMAL_ASKS)


def test_escalation_triggers_cover_five_categories() -> None:
    assert set(ESCALATION_TRIGGERS.keys()) == {
        "fraud",
        "billing_dispute",
        "regulator_complaint",
        "identity_recovery",
        "bereavement",
    }
    for cat, asks in ESCALATION_TRIGGERS.items():
        assert len(asks) >= 3, f"{cat}: at least 3 trigger phrases"
        assert all(s.strip() for s in asks)


def test_escalation_categories_match_orchestrator_enum() -> None:
    """Soak's category list must match the EscalationCategory Literal
    minus 'other'. Adding a category in one place but not the other
    is exactly the drift this test exists to catch."""
    from bss_orchestrator.types import EscalationCategory

    enum_cats = set(get_args(EscalationCategory))
    soak_cats = set(ESCALATION_TRIGGERS.keys())
    # Soak omits 'other' deliberately; it's the CSR-triaged catch-all.
    assert enum_cats - {"other"} == soak_cats


def test_cross_customer_probes_present() -> None:
    assert len(CROSS_CUSTOMER_PROBES) >= 3
    assert all(s.strip() for s in CROSS_CUSTOMER_PROBES)


def test_all_asks_flattens_every_list() -> None:
    flat = all_asks()
    expected = (
        len(NORMAL_ASKS)
        + sum(len(v) for v in ESCALATION_TRIGGERS.values())
        + len(CROSS_CUSTOMER_PROBES)
    )
    assert len(flat) == expected


# ─── Metrics + report renderer ──────────────────────────────────────


def _build_metrics() -> SoakRunMetrics:
    started = datetime(2026, 5, 1, tzinfo=timezone.utc)
    m = SoakRunMetrics(
        customers=2,
        days=1,
        started_at=started,
        ended_at=started + timedelta(minutes=2),
    )
    m.snapshot_before = DBSnapshot()
    m.snapshot_after = DBSnapshot(
        domain_event_total=20,
        ownership_violations=0,
        chat_transcript_rows=2,
        case_with_transcript_hash_rows=2,
        chat_usage_total_cost_cents=12,
        chat_usage_total_requests=4,
    )
    m.record(
        [
            TurnResult(
                customer_id="CUST-1",
                kind="dashboard",
                duration_s=0.05,
                success=True,
            ),
            TurnResult(
                customer_id="CUST-1",
                kind="chat_normal",
                duration_s=2.1,
                success=True,
            ),
            TurnResult(
                customer_id="CUST-2",
                kind="chat_normal",
                duration_s=3.4,
                success=True,
            ),
            TurnResult(
                customer_id="CUST-2",
                kind="chat_escalation:fraud",
                duration_s=4.0,
                success=True,
            ),
            TurnResult(
                customer_id="CUST-1",
                kind="cross_customer",
                duration_s=1.5,
                success=True,  # agent refused without leak
            ),
        ]
    )
    return m


def test_report_renders_pass_state() -> None:
    m = _build_metrics()
    report = render_report(m, notes="Smoke run — 2 customers × 1 day.")
    assert "v0.12 soak report" in report
    assert "✓ pass" in report
    assert "Ownership-check trips" in report
    # Latency line picks up p99 from chat turns + cross-customer.
    assert "p99" in report
    # All escalation categories the soak fired show up.
    assert "fraud" in report
    # Notes are rendered.
    assert "Smoke run" in report


def test_report_flags_ownership_violation_as_fail() -> None:
    m = _build_metrics()
    m.snapshot_after.ownership_violations = 1
    report = render_report(m)
    assert "✗ FAIL" in report
    assert "Ownership-check trips" in report


def test_report_flags_p99_alarm_at_above_5s() -> None:
    m = _build_metrics()
    # Plant a high-latency chat turn to push p99 over 5s.
    m.turns.append(
        TurnResult(
            customer_id="CUST-1",
            kind="chat_normal",
            duration_s=12.0,
            success=True,
        )
    )
    report = render_report(m)
    assert "⚠ alarm" in report or "✗ FAIL" in report


def test_report_flags_drift_above_5_percent() -> None:
    m = _build_metrics()
    # Counted = 4 successes among chat-shaped turns; recorded = 5
    # → 25% drift, well above the 5% gate.
    m.snapshot_after.chat_usage_total_requests = 5
    report = render_report(m)
    assert "⚠ drift" in report
