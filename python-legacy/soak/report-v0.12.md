# v0.12 soak report

**Generated:** 2026-04-27T09:40:42.526227+00:00
**Window:** 30 synthetic customers × 14 simulated days
**Wall-clock duration:** 1.2 minutes

## Soak gates (phases/V0_12_0.md §5.3)

| Gate | Target | Result | Status |
|---|---|---|---|
| Ownership-check trips | 0 | 0 | ✓ pass |
| p99 chat latency | < 5s (alarm 15s) | 8.35s | ⚠ alarm |
| Chat-usage drift | within 5% | counted=14 recorded=14 (0.0%) | ✓ pass |
| Cross-customer leaks | 0 | 0 | ✓ pass |
| Transcript growth | linear | 1 new rows | (advisory) |

## Activity breakdown

| Event kind | Count |
|---|---|
| `dashboard` | 35 |
| `chat_normal` | 13 |
| `chat_escalation:identity_recovery` | 1 |

### Escalations by category
- `identity_recovery`: 1

### Cross-customer probes
- attempts: 0
- agent refused without leak: 0
- trip or stream failure: 0

## Latency (chat turns only)

| Quantile | Seconds |
|---|---|
| p50 | 2.85 |
| p95 | 4.37 |
| p99 | 8.35 |
| max | 8.35 |
| count | 14 |

## DB snapshot delta

| Counter | Before | After | Δ |
|---|---|---|---|
| domain_event total | 0 | 525 | 525 |
| ownership_violation rows | 0 | 0 | 0 |
| chat_transcript rows | 0 | 1 | 1 |
| case rows with transcript_hash | 0 | 1 | 1 |
| chat_usage cost_cents (sum) | 0 | 14 | 14 |
| chat_usage requests (sum) | 0 | 14 | 14 |

## Notes

v0.12.0 internal-beta soak: 30 synthetic customers × 14 simulated days. Stack: docker-compose all-in-one, BSS_LLM_MODEL=google/gemma-4-26b-a4b-it. Run after PR11 fixes: (a) synthetic_customer SSE drainer matches 'dot done' / 'dot error' class tokens (not bare words); (b) astream_once now yields AgentEventTurnUsage BEFORE FinalMessage so chat_caps.record_chat_turn fires before SSE consumers disconnect on the 'status: done' frame.
