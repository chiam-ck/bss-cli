# Rust CSR/cockpit portal — multi-stage, distroless final. Build context = REPO ROOT
# (see the self-serve Dockerfile header): the Rust workspace is at the repo root
# post-2.0-flip, and the portal serves Jinja templates + static assets from disk under
# portals/csr/assets + crates/bss-portal-ui/assets (decoupled from the Python tree).
#
#   docker build -f portals/csr/Dockerfile -t bss-portal-csr:rust .
#
# Newer than the backend services' 1.80: the portals pull in bss-branding's logo
# processing (image 0.25 → MSRV 1.88; moxcms 0.8 → edition 2024). Matches the dev
# toolchain (1.97).
#
# cargo-chef (Phase 8): chef → planner → cook compile the ~450-crate dep graph in a
# layer keyed on Cargo.toml/Cargo.lock, so a source-only edit rebuilds just the
# workspace crates + this binary (~2 min). The chef/planner/cook stages are
# byte-identical across BOTH portal Dockerfiles, so the cook layer is built once and
# shared. python-legacy/ is excluded via .dockerignore, so `COPY . .` grabs only the
# Rust workspace. See portals/self-serve/Dockerfile + services/catalog/Dockerfile.
FROM rust:1.97-slim AS chef
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
RUN cargo build --release -p portal-csr

FROM gcr.io/distroless/cc-debian12 AS runtime
# distroless nonroot uid 65532.
USER 65532:65532
COPY --from=builder /build/target/release/portal-csr /usr/local/bin/portal-csr
# Asset trees (env overrides — the CARGO_MANIFEST_DIR defaults don't exist here).
COPY portals/csr/assets/static             /assets/local/static
COPY portals/csr/assets/templates          /assets/local/templates
COPY crates/bss-portal-ui/assets/static    /assets/shared/static
COPY crates/bss-portal-ui/assets/templates /assets/shared/templates
ENV BSS_CSR_STATIC_DIR=/assets/local/static \
    BSS_CSR_TEMPLATE_DIR=/assets/local/templates \
    BSS_PORTAL_SHARED_STATIC_DIR=/assets/shared/static \
    BSS_PORTAL_SHARED_TEMPLATE_DIR=/assets/shared/templates \
    BSS_PORTAL_CSR_PORT=8000
EXPOSE 8000
# Distroless has no curl; the binary self-probes GET /health via --healthcheck
# (mirrors the Python image's `curl -f http://localhost:8000/health || exit 1`).
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["/usr/local/bin/portal-csr", "--healthcheck"]
ENTRYPOINT ["/usr/local/bin/portal-csr"]
