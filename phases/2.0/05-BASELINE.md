# 05 — Python Baseline (the "before" measurement)

**This is the load-bearing number for the whole migration.** When 2.0 is done,
Phase 8 re-measures the same metrics on the all-Rust stack and this file is what
it compares against (definition-of-done #4 in `00-STRATEGY.md`, motto #6). Capture
it now, while the Python system is the real thing — it cannot be reconstructed
after cutover.

- **Measured:** 2026-07-11, branch `2.0` (parity baseline near Python `v1.8.1`,
  `BSS_RELEASE = "1.8.1"`).
- **Host:** Linux 7.0.0 x86_64, 4 vCPU, 15 GiB RAM, Docker 29.4.1 / Compose v5.1.3,
  Python 3.14.4 in-container.
- **Stack state:** 11 bss app containers up and healthy; all 11 HTTP surfaces
  answered `GET /health` → 200. Infra (Postgres/RabbitMQ/Jaeger) was **not**
  containerised in this environment at capture time — see caveat in §5.

> Re-run methodology is in §6 so Phase 8 measures identically. Numbers are a
> point-in-time snapshot on an idle stack (no load harness running); treat them
> as an order-of-magnitude baseline, not a benchmark suite.

---

## 1. Motto #6 budgets vs. measured

| Metric | Budget (CLAUDE.md motto #6) | Python baseline (measured) | Headroom |
|---|---|---|---|
| Full-stack RAM | < 4 GB | **1.18 GiB** app plane (11 containers) + infra (uncaptured, ~0.3–0.5 GiB typical) | comfortable |
| Cold start | < 30 s | **6.36 s** full stack (all 11 started together → last one serving) | large |
| p99 internal API latency | < 50 ms | **12.8 ms** p99 on `/health` (idle, `n=100`) | large, but see §3 |

The Rust port is expected to widen all three margins substantially (static
binaries, no interpreter, distroless images). The point of this file is to make
that improvement *provable* rather than asserted.

### Cold start breakdown (all 11, started together)

Method: `docker stop` the whole app plane, then `docker start` all 11 at once
(so they contend for the same 4 vCPUs — the honest "cold boot" case), timing each
to its first `GET /health` → 200 from a common t0.

| Service | → serving |
|---|---|
| catalog | 3.16 s |
| crm | 4.02 s |
| payment | 4.15 s |
| rating | 4.15 s |
| com | 4.47 s |
| som | 4.50 s |
| subscription | 5.09 s |
| mediation | 5.14 s |
| provisioning-sim | 5.20 s |
| portal-self-serve | 6.20 s |
| portal-csr | 6.36 s |
| **Full stack (last to serve)** | **6.36 s** |

The two Jinja/HTMX portals are the tail (they also link the orchestrator
in-process). In isolation a single service restarts in ~2 s (rating measured at
2.14 s alone); under simultaneous boot the CPU contention roughly doubles that.
6.36 s is the number that matters for motto #6.

## 2. Runtime memory footprint (app plane)

`docker stats --no-stream`, idle stack:

| Container | RSS |
|---|---|
| portal-self-serve | 155.5 MiB |
| portal-csr | 137.2 MiB |
| crm | 106.0 MiB |
| subscription | 107.3 MiB |
| payment | 104.1 MiB |
| catalog | 101.4 MiB |
| com | 100.1 MiB |
| rating | 111.6 MiB* |
| som | 96.3 MiB |
| provisioning-sim | 93.3 MiB |
| mediation | 91.3 MiB |
| **Total (11)** | **1204 MiB ≈ 1.18 GiB** |

Per-container RSS clusters at ~90–110 MiB for services, ~140–155 MiB for the two
Jinja/HTMX portals. This is dominated by the Python interpreter + FastAPI +
SQLAlchemy/asyncpg + aio-pika + OTel per process — i.e. mostly fixed per-process
overhead, which is exactly what a Rust static binary collapses.
*rating measured just after its cold-start restart, so slightly above its idle ~90 MiB.

## 3. Latency

- `/health` on rating, idle, `n=100`: **p50 0.72 ms, p95 1.55 ms, p99 12.8 ms**
  (min 0.51, max 12.8). The p99 tail is a single outlier (likely GC / event-loop
  scheduling), which is itself an interesting Rust-vs-Python data point.
- **Caveat:** `/health` bypasses routing, policy, DB, and events, so this is a
  *floor*, not the motto's "internal API p99 under load." A proper p99 needs the
  service under representative load with DB round-trips. Phase 8 should measure a
  real endpoint (e.g. a TMF read) under the scenario harness, not just `/health`.
  Recorded here as the reproducible floor; upgrade the method at cutover.

## 4. Image / disk footprint

Nominal image sizes (`docker images`), the metric a distroless Rust build should
crush:

| Image | Size |
|---|---|
| portal-self-serve | 418 MB |
| portal-csr | 372 MB |
| payment | 319 MB |
| subscription | 306 MB |
| crm | 305 MB |
| catalog | 304 MB |
| com / mediation / provisioning-sim / rating / som | 303 MB each |
| **11 bss-cli app images, nominal sum** | **≈ 3.46 GB** (shared python-slim base layers reduce true on-disk) |
| infra: `pgvector/pgvector:pg16` | 621 MB |
| infra: `postgres:16-alpine` | 420 MB |

Dockerfiles today are 2-stage `python-slim` (not distroless — they carry curl for
healthchecks + a venv, a CLAUDE.md aspiration the Python images never met). Phase 8
target: `FROM scratch`/distroless static binaries, healthcheck via the binary's own
`--healthcheck` flag. Expect per-image sizes in the tens of MB.

## 5. Static footprint (what actually ports)

| Area | Files (`*.py`) | LOC |
|---|---|---|
| services (9 + _template) | 376 | 32,899 |
| packages (`bss-*`, 15) | 210 | 29,565 |
| portals (self-serve, csr) | 104 | 23,457 |
| orchestrator | 56 | 10,703 |
| cli | 55 | 9,772 |
| **Total Python** | **819** | **109,297** |

Other counts:
- **1,735** test functions (the black-box acceptance harness that guards the port).
- **30** Alembic migrations (frozen history; baselined into sqlx at Phase 8).
- **149** locked runtime dependency packages (`uv.lock`).
- **99** distinct `BSS_*` env vars referenced (the `.env` contract).

Expected Rust outcome (doc 04 §3): ~1.1–1.4× the Python LOC → **~120–150k LOC**.

## 6. Reproduction (run these again at Phase 8 for apples-to-apples)

```bash
# Memory (app plane), idle stack:
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep bss-cli

# Image sizes:
docker images --format '{{.Repository}} {{.Size}}' | grep '^bss-cli'

# Full-stack cold start (stop all, start together, time each to HTTP 200):
docker stop -t 3 $(docker ps --filter label=com.docker.compose.project=bss-cli -q)
#   t0 just before `docker start` of all 11; poll each /health until 200;
#   report per-service time and the max (= full-stack cold start).
#   (baseline: 6.36 s full stack; ~2 s for a single service in isolation)

# Latency floor (upgrade to a real endpoint under load at Phase 8):
#   100× GET localhost:8008/health, report p50/p95/p99

# Static LOC:
find services packages portals orchestrator cli -name '*.py' \
  -not -path '*/__pycache__/*' | xargs wc -l | tail -1
```

## 7. Caveats (read before comparing)

1. **Infra not captured.** Postgres/RabbitMQ/Jaeger weren't running as local
   containers at capture time, so the 1.18 GiB is the **app plane only**. Add
   Postgres (pgvector), RabbitMQ, Jaeger for the true full-stack RAM figure — the
   Rust port doesn't replace these (they stay as-is), so the delta the migration
   is responsible for is entirely in the 1.18 GiB app plane.
2. **Idle, not under load.** All numbers are on an idle stack. The honest
   comparison at Phase 8 is idle-vs-idle for RAM/image, and under the same
   scenario load for latency.
3. **`/health` latency is a floor**, not the motto's under-load p99 (§3).
4. **Same host.** Re-measure Rust on this same 4-vCPU / 15 GiB host (or note the
   host if it changes) — footprint numbers are only comparable on matched hardware.
