# Rust som service — multi-stage, distroless final. Build context is the `rust/`
# workspace root so the path-dep crates resolve. Same port (8000) as the Python
# image it replaces.
#
#   docker build -f services/som/Dockerfile -t bss-som:rust .

FROM rust:1.80-slim AS builder
WORKDIR /build
COPY . .
RUN cargo build --release -p som

FROM gcr.io/distroless/cc-debian12 AS runtime
# distroless nonroot uid 65532.
USER 65532:65532
COPY --from=builder /build/target/release/som /usr/local/bin/som
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/som"]
