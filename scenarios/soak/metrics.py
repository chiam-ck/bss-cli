"""Metrics aggregation + report rendering for the v0.12 soak.

Per phases/V0_12_0.md §5.2, the soak monitors:

* ``audit.domain_event`` rows with event_type=``agent.ownership_violation``
  — must stay zero across the entire run.
* ``audit.chat_usage`` rows — total cost_cents and total
  requests_count (the cap accounting). Drift between the runner's
  observed-success-count and the recorded count signals a record_*
  bug.
* p99 chat-turn end-to-end latency — target <5s, alarm at 15s.
* DB row counts on ``audit.chat_transcript``, ``crm.case``,
  ``audit.domain_event`` — must not grow unbounded over the run.

The report writer renders a markdown summary the operator commits
to ``soak/report-vXX.md``. The renderer is deterministic so the
report diffs cleanly across re-runs of the same window.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .synthetic_customer import TurnResult


@dataclass
class DBSnapshot:
    """Counters sampled at the start + end of the run."""

    domain_event_total: int = 0
    ownership_violations: int = 0
    chat_transcript_rows: int = 0
    case_with_transcript_hash_rows: int = 0
    chat_usage_total_cost_cents: int = 0
    chat_usage_total_requests: int = 0


async def snapshot_db(db_url: str) -> DBSnapshot:
    """Read the four counters that bound the soak's drift checks."""
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            de_total = (
                await s.execute(text("SELECT count(*) FROM audit.domain_event"))
            ).scalar() or 0
            ownership = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM audit.domain_event "
                        "WHERE event_type = 'agent.ownership_violation'"
                    )
                )
            ).scalar() or 0
            transcripts = (
                await s.execute(text("SELECT count(*) FROM audit.chat_transcript"))
            ).scalar() or 0
            cases_linked = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM crm.case "
                        "WHERE chat_transcript_hash IS NOT NULL"
                    )
                )
            ).scalar() or 0
            cost_total = (
                await s.execute(
                    text("SELECT COALESCE(sum(cost_cents), 0) FROM audit.chat_usage")
                )
            ).scalar() or 0
            req_total = (
                await s.execute(
                    text("SELECT COALESCE(sum(requests_count), 0) FROM audit.chat_usage")
                )
            ).scalar() or 0
        return DBSnapshot(
            domain_event_total=int(de_total),
            ownership_violations=int(ownership),
            chat_transcript_rows=int(transcripts),
            case_with_transcript_hash_rows=int(cases_linked),
            chat_usage_total_cost_cents=int(cost_total),
            chat_usage_total_requests=int(req_total),
        )
    finally:
        await engine.dispose()


@dataclass
class SoakRunMetrics:
    """All observations that feed the final report."""

    customers: int = 0
    days: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    turns: list[TurnResult] = field(default_factory=list)
    snapshot_before: DBSnapshot | None = None
    snapshot_after: DBSnapshot | None = None

    def record(self, results: list[TurnResult]) -> None:
        self.turns.extend(results)

    def kinds_count(self) -> Counter[str]:
        return Counter(t.kind for t in self.turns)

    def successes(self) -> int:
        return sum(1 for t in self.turns if t.success)

    def failures(self) -> int:
        return sum(1 for t in self.turns if not t.success)

    def chat_latency_quantiles(self) -> dict[str, float]:
        chat = [
            t.duration_s
            for t in self.turns
            if t.kind.startswith("chat_") or t.kind == "cross_customer"
        ]
        if not chat:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        chat_sorted = sorted(chat)
        return {
            "count": len(chat_sorted),
            "p50": statistics.median(chat_sorted),
            "p95": _quantile(chat_sorted, 0.95),
            "p99": _quantile(chat_sorted, 0.99),
            "max": chat_sorted[-1],
        }

    def escalation_breakdown(self) -> Counter[str]:
        return Counter(
            t.kind.split(":", 1)[1]
            for t in self.turns
            if t.kind.startswith("chat_escalation:")
        )

    def cross_customer_outcomes(self) -> dict[str, int]:
        rows = [t for t in self.turns if t.kind == "cross_customer"]
        return {
            "attempts": len(rows),
            "success_no_leak": sum(1 for t in rows if t.success),
            "trip_or_failed": sum(1 for t in rows if not t.success),
        }


def _quantile(sorted_data: list[float], q: float) -> float:
    if not sorted_data:
        return 0.0
    idx = max(0, min(len(sorted_data) - 1, int(round(q * (len(sorted_data) - 1)))))
    return sorted_data[idx]


# ─── Report rendering ────────────────────────────────────────────────


_REPORT_TEMPLATE = """\
# v0.12 soak report

**Generated:** {ended_iso}
**Window:** {customers} synthetic customers × {days} simulated days
**Wall-clock duration:** {wall_min:.1f} minutes

## Soak gates (phases/V0_12_0.md §5.3)

| Gate | Target | Result | Status |
|---|---|---|---|
| Ownership-check trips | 0 | {ownership_trips} | {gate_ownership} |
| p99 chat latency | < 5s (alarm 15s) | {p99:.2f}s | {gate_p99} |
| Chat-usage drift | within 5% | counted={counted_chat} recorded={recorded_chat} ({drift_pct:.1f}%) | {gate_drift} |
| Cross-customer leaks | 0 | {cross_leaks} | {gate_cross} |
| Transcript growth | linear | {transcripts_added} new rows | (advisory) |

## Activity breakdown

| Event kind | Count |
|---|---|
{kinds_table}

### Escalations by category
{escalation_table}

### Cross-customer probes
{cross_table}

## Latency (chat turns only)

| Quantile | Seconds |
|---|---|
| p50 | {p50:.2f} |
| p95 | {p95:.2f} |
| p99 | {p99:.2f} |
| max | {lmax:.2f} |
| count | {chat_count} |

## DB snapshot delta

| Counter | Before | After | Δ |
|---|---|---|---|
| domain_event total | {de_before} | {de_after} | {de_delta} |
| ownership_violation rows | {ov_before} | {ov_after} | {ov_delta} |
| chat_transcript rows | {ct_before} | {ct_after} | {ct_delta} |
| case rows with transcript_hash | {cl_before} | {cl_after} | {cl_delta} |
| chat_usage cost_cents (sum) | {cc_before} | {cc_after} | {cc_delta} |
| chat_usage requests (sum) | {cr_before} | {cr_after} | {cr_delta} |

## Notes

{notes}
"""


def render_report(m: SoakRunMetrics, *, notes: str = "") -> str:
    """Markdown report body. Pure — accepts metrics, returns text."""
    lat = m.chat_latency_quantiles()
    before = m.snapshot_before or DBSnapshot()
    after = m.snapshot_after or DBSnapshot()
    ownership_trips = after.ownership_violations - before.ownership_violations
    transcripts_added = after.chat_transcript_rows - before.chat_transcript_rows

    counted_chat = sum(
        1
        for t in m.turns
        if t.success and (t.kind.startswith("chat_") or t.kind == "cross_customer")
    )
    recorded_chat = after.chat_usage_total_requests - before.chat_usage_total_requests
    if counted_chat == 0 and recorded_chat == 0:
        drift_pct = 0.0
    elif counted_chat == 0:
        drift_pct = 100.0
    else:
        drift_pct = abs(recorded_chat - counted_chat) / counted_chat * 100

    cross = m.cross_customer_outcomes()
    cross_leaks = cross.get("attempts", 0) - cross.get("success_no_leak", 0)

    p99 = lat["p99"]
    wall_min = 0.0
    if m.started_at and m.ended_at:
        wall_min = (m.ended_at - m.started_at).total_seconds() / 60.0

    kinds_lines = "\n".join(
        f"| `{k}` | {v} |" for k, v in m.kinds_count().most_common()
    ) or "| (no events fired) | 0 |"

    esc = m.escalation_breakdown()
    if esc:
        esc_lines = "\n".join(f"- `{k}`: {v}" for k, v in esc.most_common())
    else:
        esc_lines = "_(no escalations fired in this window — expected at low rates over short days)_"

    cross_lines = (
        f"- attempts: {cross['attempts']}\n"
        f"- agent refused without leak: {cross['success_no_leak']}\n"
        f"- trip or stream failure: {cross['trip_or_failed']}"
    )

    return _REPORT_TEMPLATE.format(
        ended_iso=(m.ended_at or datetime.utcnow()).isoformat(),
        customers=m.customers,
        days=m.days,
        wall_min=wall_min,
        ownership_trips=ownership_trips,
        gate_ownership="✓ pass" if ownership_trips == 0 else "✗ FAIL — investigate",
        p99=p99,
        gate_p99=(
            "✓ pass"
            if p99 < 5
            else ("⚠ alarm" if p99 < 15 else "✗ FAIL — p99 above 15s")
        ),
        counted_chat=counted_chat,
        recorded_chat=recorded_chat,
        drift_pct=drift_pct,
        gate_drift="✓ pass" if drift_pct <= 5 else "⚠ drift > 5% — investigate",
        cross_leaks=cross_leaks,
        gate_cross="✓ pass" if cross_leaks == 0 else "✗ FAIL — leak detected",
        transcripts_added=transcripts_added,
        kinds_table=kinds_lines,
        escalation_table=esc_lines,
        cross_table=cross_lines,
        p50=lat["p50"],
        p95=lat["p95"],
        lmax=lat["max"],
        chat_count=int(lat["count"]),
        de_before=before.domain_event_total,
        de_after=after.domain_event_total,
        de_delta=after.domain_event_total - before.domain_event_total,
        ov_before=before.ownership_violations,
        ov_after=after.ownership_violations,
        ov_delta=after.ownership_violations - before.ownership_violations,
        ct_before=before.chat_transcript_rows,
        ct_after=after.chat_transcript_rows,
        ct_delta=after.chat_transcript_rows - before.chat_transcript_rows,
        cl_before=before.case_with_transcript_hash_rows,
        cl_after=after.case_with_transcript_hash_rows,
        cl_delta=after.case_with_transcript_hash_rows - before.case_with_transcript_hash_rows,
        cc_before=before.chat_usage_total_cost_cents,
        cc_after=after.chat_usage_total_cost_cents,
        cc_delta=after.chat_usage_total_cost_cents - before.chat_usage_total_cost_cents,
        cr_before=before.chat_usage_total_requests,
        cr_after=after.chat_usage_total_requests,
        cr_delta=after.chat_usage_total_requests - before.chat_usage_total_requests,
        notes=notes or "_no operator notes_",
    )
