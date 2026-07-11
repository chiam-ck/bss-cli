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
| `bss-context` | `auth_context` / RequestCtx | ⬜ next |
| `bss-middleware` | `packages/bss-middleware` (TokenMap) | ⬜ |
| `bss-db` | pool + `PolicyViolation` | ⬜ |
| `bss-models` | `packages/bss-models` structs | ⬜ |
| `bss-events` | `packages/bss-events` relay/consumer | ⬜ |
| `bss-clients` | `packages/bss-clients` | ⬜ |
| `bss-telemetry` | `packages/bss-telemetry` | ⬜ |

See [`../phases/2.0/PROGRESS.md`](../phases/2.0/PROGRESS.md) for the running log.
