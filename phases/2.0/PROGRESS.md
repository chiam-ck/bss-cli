# Migration Progress Log

Running log of the phases/2.0 Rust port. One entry per work session. The plan
docs (`00`–`04`) are the *design*; this file is the *state*.

Branch: `2.0`. Workspace: [`../../rust/`](../../rust/).

---

## Tagging discipline (2.0)

Every phase gets an **annotated** git tag when — and only when — its exit criteria
in [`03-PHASES.md`](03-PHASES.md) are met (parity harness green on the mixed stack,
golden diffs clean, `cargo fmt` + `clippy -D warnings` + `test` green). The tag is
the "phase done" gate — consistent with the repo's "verify first, commit after /
one commit per phase minimum."

**Scheme — phase pre-releases of the final `v2.0.0`:**

| Tag | Cut / gated on |
|---|---|
| `v2.0.0-phase.0` | Foundations: 7 platform crates + CI + golden rig; hello-world conformance passes |
| `v2.0.0-phase.1` | rating cut over (pilot); per-service playbook written |
| `v2.0.0-phase.2` | mediation + provisioning-sim + som cut over |
| `v2.0.0-phase.3` | catalog + com cut over |
| `v2.0.0-phase.4` | **bilingual resting point** — all 9 services Rust; portals/orch/CLI still Python. Shippable pause point (strategy §5); re-measure motto #6 for the service plane vs [`05-BASELINE.md`](05-BASELINE.md) |
| `v2.0.0-phase.5` | orchestrator lib + knowledge + cockpit-core (no deployable cutover; fixture-parity green) |
| `v2.0.0-phase.6` | portals (self-serve, csr) cut over |
| `v2.0.0-phase.7` | CLI + REPL + scenarios cut over |
| `v2.0.0` | **final cutover** — all-Rust, Alembic retired, 14-day soak passed (Phase 8) |

SemVer ordering holds: `2.0.0-phase.0 < 2.0.0-phase.1 < … < 2.0.0-phase.7 < 2.0.0`
(numeric pre-release identifiers order numerically; any pre-release precedes the
release). The major bump to `2.0.0` marks the platform rewrite even though wire
contracts are frozen (§3) — the migration is behaviour-frozen, not API-versioned.

**Mechanics:**
- `git tag -a v2.0.0-phase.N -m "<phase>: <what cut over>; exit criteria met (<evidence>)"` — annotated so the message records the exit-criteria evidence.
- Tag the commit on `2.0` that *completes* the phase (post-merge if the phase ran on a feature branch). **Mid-phase commits are never tagged** — e.g. this scaffold commit is *not* `phase.0`; that tag waits until all seven crates + CI + the golden rig are done.
- Intra-phase service cutovers (P2 ×3, P4 ×3) are **commits, not tags** (`feat(payment): rust cutover`); the phase tag caps the set. If one service must be pinned for a prod canary, use an incrementing pre-release: `v2.0.0-phase.4.1`, `.2`, `.3`.
- The Python parity baseline stays `v1.8.1` on mainline; every 2.0 tag is `v2.0.0-*`, so they never collide.

---

## Phase 0 — Foundations — IN PROGRESS

Goal: Cargo workspace + CI + the seven platform crates against a throwaway
hello-world service (see `03-PHASES.md`).

### Done

- **Python baseline captured** → [`05-BASELINE.md`](05-BASELINE.md). The "before"
  measurement for motto #6, taken while the Python stack was live (it can't be
  reconstructed post-cutover). Headlines: **1.18 GiB** app-plane RAM (11
  containers), **6.36 s** full-stack cold start (all 11 booted together;
  per-service breakdown in the doc), **12.8 ms** p99 on `/health`, **~3.46 GB**
  nominal image sum, **109,297** LOC Python. Phase 8 re-measures the same way
  (§6 of that doc) and this is the comparison point.
- **Toolchain + scaffold.** rustup stable (1.97) with rustfmt + clippy. Cargo
  workspace at `rust/` (D7: subtree, not standalone repo — rationale in
  `rust/README.md`). Workspace lints: `unsafe_code = forbid`,
  clippy `unwrap_used`/`expect_used = warn` (promoted to deny by `-D warnings`).
- **CI from day one.** `.github/workflows/rust.yml` — fmt + clippy `-D warnings`
  + test on `2.0` pushes / PRs touching `rust/**`. (Closes the "no CI anywhere"
  gap the inventory flagged; sqlx-prepare job added when `bss-db` lands.)
- **`bss-clock`** (first crate — "everything reads it"). Faithful port of
  `packages/bss-clock`:
  - `now/freeze/unfreeze/advance/state/parse_duration/reset_for_tests`, wall &
    frozen modes. Process-global state via `ArcSwap<Inner>` with `rcu` writers
    (§2.2 of `02-TECH-MAPPING.md`) → lock-free `now()` reads.
  - `clock_admin_router()` (axum) mirrors the FastAPI router: `GET /clock/now`
    unguarded; `POST freeze|unfreeze|advance` gated on `BSS_ALLOW_ADMIN_RESET`;
    camelCase wire shape (`offsetSeconds`/`frozenAt`), RFC-3339 instants,
    `{"detail":{code,message}}` errors, 403/422 parity.
  - 15 integration tests porting `tests/test_clock.py` 1:1 (serialized on a
    process-global `Mutex` since the clock is a singleton). All green; fmt +
    clippy clean.

### Next (Phase 0 remainder)

1. `bss-context` — `RequestCtx` struct + `tokio::task_local!` scope; header
   extraction (`x-request-id`/`x-bss-actor`/`x-bss-channel`/`x-bss-tenant`).
   Establishes the §2.1 ContextVar translation the whole tree depends on.
2. `bss-middleware` — `TokenMap` (HMAC-hashed, constant-time full-scan,
   env-name→identity), `/health*` + `/webhooks/` exemptions, sentinel/length
   validation. Needs golden HMAC vectors captured from the Python oracle first.
3. `bss-db`, `bss-models`, `bss-events`, `bss-clients`, `bss-telemetry`.
4. Golden-contract capture rig + hello-world conformance service.

### Notes / decisions taken

- **Deps pinned minimal:** chrono, arc-swap, serde_json, axum (+ tokio/tower dev).
  No `regex` — `parse_duration` is hand-rolled to match `^\s*(\d+)\s*([smhd])\s*$`
  without the dependency.
- Clock tests need `--test-threads` safety: solved in-crate with a serialising
  `Mutex` + `reset_for_tests()`, not by constraining the runner.
