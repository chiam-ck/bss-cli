# Rust crm service (Phase 4c) — multi-stage, distroless final. Build context is the
# `rust/` workspace root so the path-dep crates resolve. Same port (8000) as the
# Python image it replaces.
#
#   docker build -f services/crm/Dockerfile -t bss-crm:rust .
#
# distroless has no shell/curl; compose supplies the healthcheck externally
# (Phase 8 finalizes the `--healthcheck` flag approach).

FROM rust:1.80-slim AS builder
WORKDIR /build
COPY . .
RUN cargo build --release -p crm

FROM gcr.io/distroless/cc-debian12 AS runtime
USER 65532:65532
COPY --from=builder /build/target/release/crm /usr/local/bin/crm
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/crm"]
