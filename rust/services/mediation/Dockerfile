# Rust mediation service — multi-stage, distroless final (doctrine: non-root,
# distroless where practical). Build context is the `rust/` workspace root so the
# path-dep crates resolve. Same port (8000) as the Python image it replaces.
#
#   docker build -f services/mediation/Dockerfile -t bss-mediation:rust .
#
# distroless has no shell/curl; compose supplies the healthcheck externally
# (Phase 8 finalizes the `--healthcheck` flag approach). See phases/2.0/03-PHASES.md.

FROM rust:1.80-slim AS builder
WORKDIR /build
COPY . .
RUN cargo build --release -p mediation

FROM gcr.io/distroless/cc-debian12 AS runtime
# distroless nonroot uid 65532.
USER 65532:65532
COPY --from=builder /build/target/release/mediation /usr/local/bin/mediation
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/mediation"]
