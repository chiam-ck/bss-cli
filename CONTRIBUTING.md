# Contributing to BSS-CLI

> If you're reading this for the first time, **read [`CLAUDE.md`](CLAUDE.md) before making any code changes.** It's the project doctrine — seven motto principles, scope boundaries, write-policy doctrine, naming conventions, anti-patterns. Everything else here assumes you've internalized that contract.

> **Stack note (2.0).** BSS-CLI is an **all-Rust** Cargo workspace at the repo root (`cargo`, and `make test`/`fmt`/`lint`/`doctrine-check` all drive Rust). The retired Python implementation lives in `python-legacy/` as the reproducible **oracle**; its dev loop is the `py-*` make targets (`make py-test`, `py-fmt`, `py-migrate`, …). `CLAUDE.md`'s "Tech stack" section still lists the Python originals (FastAPI/SQLAlchemy/pytest/…) pending a doctrine amendment — the behaviour, contracts, data model, and tool surface are unchanged by the port.

## Project doctrine

`CLAUDE.md` is the contract. Three things matter most:

1. **Bundled-prepaid only, block-on-exhaust, card-on-file mandatory.** No proration, no dunning, no credit risk.
2. **Write through policy, read freely.** Every mutation flows through a policy layer that enforces domain invariants. The LLM cannot corrupt state even when asked to.
3. **CLI-first, LLM-native.** Every operation is a tool the LLM can call. The terminal is the primary UI. Portals are channels onto the same tool registry, not a parallel write path.

Anything that contradicts these requires a Phase 0 amendment in `CLAUDE.md`. Anything that *adds* to them is welcome — open a `DECISIONS.md` entry first.

## Phase discipline

Substantive change lands under a numbered phase or version spec in `phases/`. Pre-v0.1 work used `PHASE_01.md` through `PHASE_10.md`. Post-v0.1 minor versions use `V0_X_0.md`.

A phase/version spec has this shape (template established v0.2 → v0.6):

```
# vX.Y.Z — short title

> One-paragraph framing of what this version changes and why.

## Goal
  ~3 numbered points stating the visible outcomes

## Deliverables
  - Track-by-track breakdown
  - Each deliverable has a §number, a constraint, and a verification path

## Doctrine and constraints
  Numbered list of invariants this version must not break

## Test strategy
  How the version proves itself green; specific commands

### Verification greps
  Copy-pasteable rg/grep commands the reviewer runs

## Verification checklist
  Tickable list mirroring the deliverables

## Out of scope
  Explicit list of "not in this version" items so reviewers don't ask
  for them

## Session prompt (paste verbatim when starting implementation)
  > A self-contained prompt that tells a fresh Claude session
  > exactly what to read first, what to verify before coding,
  > and what's non-negotiable.

## The trap
  Things that have gone wrong before and how to avoid them this time
```

Commit messages use Conventional Commits: `feat(vX.Y.Z): subject`, `fix(vX.Y.Z): subject`, `docs(vX.Y.Z): subject`, `refactor(vX.Y.Z): subject`. Pre-v0.1 work uses `feat(phase-NN): subject`.

## The DECISIONS.md pattern

Every non-obvious choice gets an entry in `DECISIONS.md`. The template:

```markdown
## YYYY-MM-DD — Phase N or vX.Y.Z — Title
**Context:** what prompted the decision
**Decision:** what we chose
**Alternatives:** what we rejected and why
**Consequences:** what this makes easier/harder
```

Append, don't update. If a decision is later reversed or refined, add a new entry referring back to the original. This keeps `DECISIONS.md` a faithful timeline rather than a curated narrative — future maintainers need both the current state *and* the path that got us there.

The bar for an entry is "would a stranger reading the code be confused why we did this?" If yes, write it down. If the code is self-evident, don't.

## Testing expectations

Established across Phase 4 + revisited every minor version since:

- **Every endpoint has an httpx API test** that hits the route via `AsyncClient` with `ASGITransport`. JSON payloads use **camelCase** (real wire format), not snake_case shortcuts. The test fixture in each service's `tests/conftest.py` mocks downstream clients but exercises the full FastAPI stack including middleware.
- **Every state machine has a parametrized transitions test** that walks the full transition table — for each state, every legal transition succeeds and every illegal one raises the documented error.
- **Integration tests use unique fields** generated via `uuid.uuid4().hex[:8]` (or per-test scoping) so re-runs don't collide on `email_unique` or `msisdn_unique` policies. The fix that introduced this rule is in `DECISIONS.md` 2026-04-12 — Phase 8 — *"Idempotent integration tests via UUID-suffixed emails"*.
- **Cross-service tests use respx** to mock the downstream HTTP surface unless they're explicitly tagged `@pytest.mark.integration` (skip with `-m "not integration"`).
- **Hero scenarios are end-to-end** — they hit the real compose stack and assert state via `subscription.get`, `interaction.list`, `bss trace`, etc. LLM-driven hero scenarios pass three runs in a row before any version tag.

## How to run the full suite

```bash
make test                 # cargo test --workspace (Rust). py-test = the Python oracle's per-package pytest sweep
make scenarios            # every scenario (17 heroes as of v0.18)
make scenarios-hero       # the hero-tagged scenarios (deterministic + LLM-driven mix)
make doctrine-check       # 14 grep guards: datetime, OTel surfaces, channel attribution, renewal worker confinement, etc.
```

## The hero scenario gate

The single hardest gate before tagging is **three consecutive passing runs of the LLM-driven hero scenarios.** The deterministic ones either pass or don't; the LLM ones (`llm_troubleshoot_blocked_subscription`, `portal_self_serve_signup`, `portal_csr_blocked_diagnosis`) carry model variance and have to be flake-free.

If a scenario passes 2 of 3 runs, **don't tag.** Either the model is on the edge of its reliability window for the prompt (fix the prompt) or there's a real concurrency/timing bug (fix the code). Lowering the gate is the wrong fix.

## Branching

- `main` is shippable. Every commit on `main` should pass `make test` and `make scenarios-hero`.
- Feature branches per phase or version: `feat/v0.X.Y` or `feat/phase-NN`.
- One PR per branch, into `main`. Multi-step releases keep the branch open with multiple commits; one final PR.
- Pull-request review is the merge gate. CI runs `make test` + the doctrine grep suite + the deterministic hero scenarios. The LLM scenarios are run locally before tagging (CI cost + nondeterminism — they don't gate the PR).

## What NOT to touch without amendment

These files require an explicit Phase 0 amendment to modify:

- `CLAUDE.md` — project doctrine
- `DATA_MODEL.md` — table shapes
- `ARCHITECTURE.md` — only surgical drift fixes; no structural rewrites
- `TOOL_SURFACE.md` — only sync to actual `TOOL_REGISTRY`; no aspirational additions

A Phase 0 amendment is a separate, named PR with its own DECISIONS entry stating context + change. It's not a casual edit.

## How to add a new service

Clone `services/_template/` (the post-v0.6 standardized structure):

```
services/<svc>/
├── pyproject.toml          # bss-* deps marked workspace=true
├── Dockerfile              # the workspace-rewrite sed pattern
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI factory, lifespan, middleware registration
│   ├── config.py           # _REPO_ROOT pattern (Phase 3)
│   ├── auth_context.py     # ContextVar populated by RequestIdMiddleware
│   ├── lifespan.py         # configure_telemetry + validate_api_token_present (v0.3)
│   ├── logging.py          # structlog config
│   ├── api/                # FastAPI routers; one file per TMF API
│   ├── services/           # service classes that orchestrate policies + repos
│   ├── policies/           # invariant enforcement; raises PolicyViolation
│   ├── repositories/       # SQLAlchemy CRUD over ORM models
│   ├── domain/             # ORM models if not in bss-models package
│   └── events/             # publishers + consumers (aio-pika)
└── tests/
    ├── conftest.py         # ASGI client + rolled-back transaction fixture
    └── test_*.py
```

Wiring checklist:

1. Add the service to `docker-compose.yml` (port from the 8xxx range; portals get 9xxx)
2. Add a `bss-clients/<svc>.py` client module
3. Add the service's tools to `orchestrator/bss_orchestrator/tools/<svc>.py` and register them
4. Update `TOOL_SURFACE.md` with the new tools
5. Add `BSS_<SVC>_URL` to `orchestrator/bss_orchestrator/config.py`
6. Add the service to `Makefile`'s `test:` loop
7. Document any new policies in `DECISIONS.md`

For test patterns the new service needs to follow, see `services/crm/` (richest example) or `services/payment/` (full integration test setup).

## A few last things

- **Don't add backwards-compatibility shims.** If you're changing an internal interface, change every caller in the same commit. There's no external consumer to preserve compatibility for; the whole repo moves together.
- **Don't add dead code or stub-out half-finished work.** Either complete the change or revert it. A `TODO` is fine if it points at a numbered phase; a `# will fix later` comment is not.
- **Don't reach for a framework when a function will do.** The whole project is ~20k lines of Python; a 200-line policy module that makes the doctrine readable is better than a 30-line policy DSL that requires a 500-line interpreter.
- **Read the doctrine.** `CLAUDE.md` exists because we kept making the same mistakes until we wrote them down.
