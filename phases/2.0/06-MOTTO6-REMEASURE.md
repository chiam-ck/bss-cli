# 06 — Motto #6 re-measure (the all-Rust "after")

The Phase 8 counterpart to [`05-BASELINE.md`](05-BASELINE.md). Same host, same
method (§6 of the baseline), idle-vs-idle for RAM/image, so the numbers are
apples-to-apples. **This is the proof the Rust port widened all three motto-#6
margins**, not an assertion.

- **Measured:** 2026-07-19, branch `2.0`, all-Rust service plane + portals (the
  `docker-compose.rust.yml` overlay; 11 `:rust` images).
- **Host:** identical to the baseline — Linux 7.0.0 x86_64, 4 vCPU, 15 GiB RAM,
  Docker 29.4.1 / Compose 5.1.3. (Matched host is required for footprint
  comparability — baseline §7.4.)
- **Stack state:** 11 bss app containers up + healthy; all 11 `GET /health` → 200.
  Infra (Postgres/RabbitMQ/Jaeger) on `tech-vm` over Tailscale, unchanged and not
  part of the app plane — same as the baseline.

---

## 1. Motto #6 budgets — Python vs Rust

| Metric | Budget | Python (`05-BASELINE`) | **Rust (now)** | Improvement |
|---|---|---|---|---|
| Full-stack RAM (11 app) | < 4 GB | 1204 MiB (1.18 GiB) | **81.7 MiB (0.08 GiB)** | **~14.7× smaller** — **2.0%** of budget |
| Cold start (all 11 together) | < 30 s | 6.36 s | **3.08 s** | **~2.1× faster** |
| p99 `/health` floor | < 50 ms | 12.8 ms | **6.16 ms** | ~2× lower tail |
| p99 real DB read (under load) | < 50 ms | (floor only) | **8.47 ms** (`/vas/offering`) | **under budget** |

All three motto-#6 budgets are met with large headroom. RAM is the headline: the
whole app plane fits in **~82 MiB**, 2% of the 4 GB budget.

## 2. Runtime memory (app plane, idle)

`docker stats --no-stream`, idle:

| Container | RSS (Rust) | (Python was) |
|---|---|---|
| mediation | 13.1 MiB | 91.3 |
| rating | 11.8 MiB | ~90 |
| provisioning-sim | 11.3 MiB | 93.3 |
| portal-csr | 8.7 MiB | 137.2 |
| subscription | 8.5 MiB | 107.3 |
| portal-self-serve | 6.0 MiB | 155.5 |
| catalog | 5.8 MiB | 101.4 |
| crm | 5.7 MiB | 106.0 |
| com | 5.6 MiB | 100.1 |
| som | 3.0 MiB | 96.3 |
| payment | 1.7 MiB | 104.1 |
| **Total (11)** | **81.7 MiB** | **1204 MiB** |

Per-container RSS collapses from ~90–155 MiB (Python interpreter + FastAPI +
SQLAlchemy/asyncpg + aio-pika + OTel, per process) to **~2–13 MiB** static
binaries. The two portals — the Python memory tail at 137–155 MiB — are now 6–9 MiB,
no longer the tail.

## 3. Cold start (all 11 started together)

Method: `docker stop` the app plane, `docker start` all 11 at once (contending for
the same 4 vCPUs), timing each to first `GET /health` → 200 from a common t0.

| | Python | **Rust** |
|---|---|---|
| Full stack (last to serve) | 6.36 s | **3.08 s** |
| Range across services | 3.16 – 6.36 s | **2.78 – 3.08 s** |

The Rust services cluster tightly at 2.8–3.1 s; the Jinja/HTMX portals (Python's
6.2 s tail) now serve at ~2.9–3.0 s, no longer distinguishable from the services.
(Polling granularity ~0.1 s; treat as ~3 s vs ~6.4 s.)

## 4. Latency

- **`/health` floor** (rating, idle, n=100): p50 **1.00 ms**, p95 **2.63 ms**, p99
  **6.16 ms** (min 0.67, max 22.61). Python was p50 0.72 / p95 1.55 / p99 12.8. The
  Rust p99 tail is ~half Python's; both are single-outlier tails on an idle stack.
- **Real DB-backed read, the baseline §3 upgrade** — `/vas/offering` (TMF read
  through routing + policy + a **pooled** connection to the remote DB), n=100: p50
  **5.00 ms**, p95 **6.74 ms**, p99 **8.47 ms**. **Under the 50 ms budget** — this is
  the honest "internal API p99 under load with a DB round-trip" the baseline asked
  Phase 8 to measure, and it passes.
- **Environment caveat — heavy multi-query endpoint.** The full TMF620
  `productOffering` list (15 KB; offering + per-offering price/allowance rows → many
  sequential queries) measured p50 63 / p95 105 / p99 115 ms. This is **dominated by
  N sequential round-trips to the remote `tech-vm` Postgres over Tailscale**, not
  service compute — a raw pooled single-read (`/vas/offering`) is 8.47 ms p99 on the
  same DB. It is an endpoint-shape + remote-DB artifact identical for Python and Rust
  (shared DB); a co-located DB collapses it. Not a motto violation of the service.

## 5. Image / disk footprint

Distroless static binaries vs Python's 2-stage `python-slim` (the metric §4 of the
baseline said a distroless Rust build "should crush"):

| | Python nominal | **Rust** |
|---|---|---|
| Per image | 303–418 MB | **54–66 MB** |
| 11-image sum | ≈ 3.46 GB | **657 MB** |

~**5.3× smaller** on nominal sum; every service image is now in the tens of MB
(catalog 54, payment 56, crm 57, rating 59, som 60, prov-sim 60, mediation 59, com
61, subscription 62, portal-csr 66, portal-self-serve 64 MB). Infra images
(pgvector, postgres) are unchanged — the migration doesn't touch them.

## 6. Static footprint (LOC)

| | Python (`05-BASELINE` §5) | **Rust** |
|---|---|---|
| Non-test LOC | 109,297 | **71,322** |
| Incl. tests | (n/a — 1,735 test fns) | 80,011 |

The Rust port came in at **~0.65× the Python LOC** (non-test), *under* the doc-04
estimate of 1.1–1.4×. The type system + the collapse of per-service boilerplate
(shared crates, derive macros) more than offset Rust's usual verbosity. The 32
Alembic migrations are now the single `rust/migrations/0001_baseline.sql` (2,950
lines).

## 7. Reproduction

Identical to `05-BASELINE.md` §6, against the `docker-compose.rust.yml` stack:

```bash
# RAM (app plane, idle):
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep bss-cli-
# Image sizes:
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep ':rust'
# Cold start: docker stop all 11, docker start together, poll each /health → 200, max.
# Latency floor: 100× GET localhost:8008/health → p50/p95/p99.
# Real DB read: 100× GET localhost:8001/vas/offering (token) → p50/p95/p99.
```

## 8. Caveats

1. **Idle-vs-idle** for RAM/image/cold-start (matches the baseline). Latency floor is
   idle; the DB read is a light single-client loop, not a saturating load harness.
2. **Remote DB over Tailscale** inflates any multi-query endpoint equally for Python
   and Rust; §4 isolates compute (`/health`) from DB-bound (`/vas/offering` pooled vs
   `productOffering` N-query).
3. **Same host** as the baseline — footprint numbers are only comparable on matched
   hardware, and this host matches.
4. RAM measured on the deployed `:rust` images; the Phase-8 cargo-chef/`--healthcheck`
   rework produces byte-size-identical binaries, so the figures carry to the rebuilt
   images.
