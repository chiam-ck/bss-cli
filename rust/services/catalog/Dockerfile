# Rust catalog service (Phase 3) — multi-stage, distroless final. Build context is
# the `rust/` workspace root so the path-dep crates resolve. Same port (8000) as
# the Python image it replaces.
#
#   docker build -f services/catalog/Dockerfile -t bss-catalog:rust .
#
# distroless has no shell/curl; compose supplies the healthcheck externally
# (Phase 8 finalizes the `--healthcheck` flag approach).
#
# cargo-chef layout (Phase 8): the chef → planner → cook stages compile the
# ~450-crate dependency graph in a layer keyed only on Cargo.toml/Cargo.lock, so a
# source-only edit rebuilds just the workspace crates + this binary (~2 min) instead
# of the whole tree (~20–30 min). The chef/planner/cook stages are byte-identical
# across all 9 service Dockerfiles (whole-workspace cook, no `-p`), so BuildKit
# compiles the deps ONCE and every service reuses the same cook layer.
#
# The chef stage COPYs rust-toolchain.toml (channel = stable) BEFORE installing
# cargo-chef, so every stage — install, cook, build — runs under the SAME stable
# toolchain. Without it the chef stage runs on the image's 1.80 cargo, which can't
# parse edition2024 deps (cargo-chef's own tree) and would cook artifacts a stable
# build stage then discards.

FROM rust:1.80-slim AS chef
WORKDIR /build
COPY rust-toolchain.toml .
RUN rustup show >/dev/null && cargo install cargo-chef --locked

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
COPY --from=planner /build/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY . .
RUN cargo build --release -p catalog

FROM gcr.io/distroless/cc-debian12 AS runtime
USER 65532:65532
COPY --from=builder /build/target/release/catalog /usr/local/bin/catalog
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/catalog"]
