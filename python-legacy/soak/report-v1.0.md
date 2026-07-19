# v0.12 soak report

**Generated:** 2026-05-04T03:39:57.219403+00:00
**Window:** 30 synthetic customers × 14 simulated days
**Wall-clock duration:** 1.1 minutes

## Soak gates (phases/V0_12_0.md §5.3)

| Gate | Target | Result | Status |
|---|---|---|---|
| Ownership-check trips | 0 | 0 | ✓ pass |
| p99 chat latency | < 5s (alarm 15s) | 5.77s | ⚠ alarm |
| Chat-usage drift | within 5% | counted=17 recorded=17 (0.0%) | ✓ pass |
| Cross-customer leaks | 0 | 0 | ✓ pass |
| Transcript growth | linear | 2 new rows | (advisory) |

## Activity breakdown

| Event kind | Count |
|---|---|
| `dashboard` | 37 |
| `chat_normal` | 15 |
| `chat_escalation:billing_dispute` | 1 |
| `chat_escalation:identity_recovery` | 1 |

### Escalations by category
- `billing_dispute`: 1
- `identity_recovery`: 1

### Cross-customer probes
- attempts: 0
- agent refused without leak: 0
- trip or stream failure: 0

## Latency (chat turns only)

| Quantile | Seconds |
|---|---|
| p50 | 2.77 |
| p95 | 5.62 |
| p99 | 5.77 |
| max | 5.77 |
| count | 17 |

## DB snapshot delta

| Counter | Before | After | Δ |
|---|---|---|---|
| domain_event total | 18885 | 19423 | 538 |
| ownership_violation rows | 0 | 0 | 0 |
| chat_transcript rows | 152 | 154 | 2 |
| case rows with transcript_hash | 0 | 2 | 2 |
| chat_usage cost_cents (sum) | 358 | 375 | 17 |
| chat_usage requests (sum) | 358 | 375 | 17 |

## Notes

_no operator notes_
