# bss-cli — Rust migration workspace (phases/2.0)

Cargo workspace for the strangler-fig port of bss-cli to Rust. The plan lives in
[`../phases/2.0/`](../phases/2.0/) — read `00-STRATEGY.md` first. The Python repo
in the parent directory stays runnable as the **behavioural oracle** throughout;
nothing here changes an external contract until Phase 8.

## Layout

```
rust/
├── Cargo.toml         # [workspace]; members added phase by phase
├── rust-toolchain.toml
├── crates/            # packages/bss-* equivalents (lib crates)  — P0, P5
├── services/          # 9 bin crates                             — P1–P4  (not yet)
├── portals/           # 2 bin crates                             — P6     (not yet)
└── cli/               # bss bin                                  — P7     (not yet)
```

**Repo-home decision (D7):** the plan floats either a standalone `bss-cli-rust`
repo or a `rust/` subtree in this monorepo. We took the **subtree** — it keeps the
Python oracle, scenario YAML, prompts, and templates co-located during the
transition (which every phase's parity harness needs) and matches the on-branch
(`2.0`) workflow. Revisit at Phase 8 when the Python tree is archived.

## Commands

```bash
cd rust
cargo test                                        # all crates
cargo fmt --all --check                           # formatting gate (CI)
cargo clippy --all-targets -- -D warnings         # lint gate (CI)
```

CI: `.github/workflows/rust.yml` runs fmt + clippy + test on `2.0` pushes and PRs
touching `rust/**`.

## Status — Phase 0 (Foundations)

| Crate | Ports | Status |
|---|---|---|
| `bss-clock` | `packages/bss-clock` | ✅ done — clock + admin router, 15 tests (ports `test_clock.py` 1:1), fmt + clippy clean |
| `bss-context` | `auth_context` / RequestCtx | ✅ done — `RequestCtx` + task-local scope + axum propagate layer; 10 tests, fmt + clippy clean |
| `bss-middleware` | `packages/bss-middleware` (TokenMap) | ✅ done — HMAC TokenMap + constant-time lookup + axum token gate; 28 tests incl. golden-vector conformance vs Python; fmt + clippy clean |
| `bss-db` | pool + `PolicyViolation` | ✅ done — sqlx PgPool (5+5) + typed `PolicyViolation` (IntoResponse 422, wire round-trip); 7 tests, clean |
| `bss-models` | `packages/bss-models` structs | ◐ started — `BSS_RELEASE` const (guard #14); per-table structs land per-service |
| `bss-events` | `packages/bss-events` relay/consumer | ◐ core done — staging + drain orchestration + retry/park + topology contract; 8 tests. lapin/sqlx binding with conformance service |
| `bss-clients` | `packages/bss-clients` | ◐ base done — reqwest client (timeouts, no-retry, typed errors, ctx propagation) + AuthProviders; 11 tests. 12 typed clients land per-phase |
| `bss-telemetry` | `packages/bss-telemetry` | ◐ rules done — redaction rules + semconv attr keys (4 tests); tracing/OTel bootstrap lands with conformance service |

See [`../phases/2.0/PROGRESS.md`](../phases/2.0/PROGRESS.md) for the running log.
