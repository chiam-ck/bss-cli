# BSS-CLI → Rust Migration Plan

This directory contains the migration plan for porting **bss-cli** (a ~109k-LOC Python,
SID-aligned, TMF-compliant bundled-prepaid BSS) to Rust. It is a *plan*, not code — nothing
in `~/repo/bss-cli` has been touched, and no Rust has been scaffolded yet.

## How to read this

| Doc | What it answers |
|---|---|
| [00-STRATEGY.md](00-STRATEGY.md) | The thought process: why strangler-fig over big-bang, what the invariant boundaries are, what deliberately stays Python, what "done" means. **Read this first.** |
| [01-INVENTORY.md](01-INVENTORY.md) | What actually exists in the Python repo: components, LOC, dependency layering, the load-bearing patterns a port must reproduce. |
| [02-TECH-MAPPING.md](02-TECH-MAPPING.md) | Python→Rust technology mapping (FastAPI→axum, SQLAlchemy→sqlx, LangGraph→hand-rolled ReAct, …) and the translation strategy for the six genuinely hard Python idioms. |
| [03-PHASES.md](03-PHASES.md) | The phase plan: 9 phases (0–8), each with scope, deliverables, exit criteria, sizing, and what can run in parallel. |
| [04-RISKS-AND-DECISIONS.md](04-RISKS-AND-DECISIONS.md) | Risk register, the open decisions that need a human call before/during the port, and the effort estimate with its error bars. |
| [05-BASELINE.md](05-BASELINE.md) | The **Python "before" measurement** (RAM, cold start, latency, image + LOC footprint), captured 2026-07-11. The fixed comparison point Phase 8 re-measures against for motto #6. |
| [PROGRESS.md](PROGRESS.md) | Running phase-by-phase execution log (state, not design). |

## Executive summary

- **Strategy: strangler-fig, service-by-service, behind frozen wire contracts.** The Python
  system's own doctrine makes this cheap: services talk only via HTTP + RabbitMQ + their own
  Postgres schemas — never shared objects, never shared tables. A Rust service can replace a
  Python one behind the same port and API with zero changes anywhere else. The Postgres schema,
  the RabbitMQ topology (`bss.events` + retry/parked), the `.env` contract, and the TMF payload
  shapes are the four frozen contracts.
- **The acceptance harness already exists.** The 19 hero scenarios, the Playwright e2e suite,
  and the ~21 `make doctrine-check` grep guards are black-box or pattern-level specs. The Python
  repo stays runnable as the *oracle* throughout; every phase's exit criterion is "swap the
  container image, hero scenarios still pass."
- **Phase order = platform crates → smallest service (rating) as pilot → event-plane services →
  catalog/COM → the big three (payment, subscription, CRM) → orchestrator → portals → CLI/REPL →
  cutover.** Orchestrator ports *before* the portals because both portals and the CLI link it
  in-process — porting it later would force a temporary network seam the doctrine forbids.
- **The hardest ports are not where they look.** LangGraph usage is thin (one
  `create_react_agent` call); the real orchestrator risk is ~1,500 LOC of hand-written stream
  interpretation, safety gating, ownership trip-wires, and anti-mimicry guards. The other two
  pervasive hazards are `ContextVar` ambient auth/trace context and the process-global mutable
  clock — both get explicit Rust translations (tower layers + task-locals; `ArcSwap` clock
  handle).
- **Some things deliberately don't port:** the Playwright e2e harness stays Python (it tests
  browsers, not Python); Alembic stays the migration authority until the last Python service is
  gone; HTMX/templates/prompts/scenario YAML carry over as assets, not rewrites.
- **Effort: ~55–77 person-weeks** for one experienced Rust engineer (≈ 12–18 months solo,
  ≈ 7–10 months with a second engineer joining after the pilot phase). Error bars and the
  reasoning behind them are in doc 04.
