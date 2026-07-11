# Rust rating service — multi-stage, distroless final (doctrine: non-root,
# distroless where practical). Build context is the `rust/` workspace root so the
# path-dep crates resolve. Same port (8000) as the Python image it replaces.
#
#   docker build -f services/rating/Dockerfile -t bss-rating:rust .
#
# The healthcheck hits /health with the binary itself would be ideal, but
# distroless has no shell/curl; compose supplies the healthcheck externally
# (Phase 8 finalizes the `--healthcheck` flag approach). See phases/2.0/03-PHASES.md.

FROM rust:1.80-slim AS builder
WORKDIR /build
# Cache deps: copy the manifests first. (A full workspace copy is simplest and
# still correct; the P8 image pass tightens layer caching.)
COPY . .
RUN cargo build --release -p rating

FROM gcr.io/distroless/cc-debian12 AS runtime
# distroless nonroot uid 65532.
USER 65532:65532
COPY --from=builder /build/target/release/rating /usr/local/bin/rating
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/rating"]
