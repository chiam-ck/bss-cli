# Rust payment service (Phase 4) — multi-stage, distroless final. Build context is the
# `rust/` workspace root so the path-dep crates resolve. Same port (8000) as the
# Python image it replaces.
#
#   docker build -f services/payment/Dockerfile -t bss-payment:rust .
#
# distroless has no shell/curl; compose supplies the healthcheck externally
# (Phase 8 finalizes the `--healthcheck` flag approach).

# cargo-chef (Phase 8): chef → planner → cook stages are byte-identical across all
# 9 service Dockerfiles, so BuildKit compiles the ~450-crate dep graph ONCE and
# every service reuses the cook layer; a source-only edit rebuilds just the
# workspace crates + this binary. See services/catalog/Dockerfile for the rationale.
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
RUN cargo build --release -p payment

FROM gcr.io/distroless/cc-debian12 AS runtime
USER 65532:65532
COPY --from=builder /build/target/release/payment /usr/local/bin/payment
EXPOSE 8000
# Distroless has no curl; the binary self-probes GET /health via --healthcheck
# (mirrors the Python image's `curl -f http://localhost:8000/health || exit 1`).
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["/usr/local/bin/payment", "--healthcheck"]
ENTRYPOINT ["/usr/local/bin/payment"]
