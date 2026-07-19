# Rust CSR/cockpit portal — multi-stage, distroless final. Build context = REPO ROOT
# (see the self-serve Dockerfile header): the portal serves Jinja templates + static
# assets from disk, reused verbatim from the Python source tree.
#
#   docker build -f rust/portals/csr/Dockerfile -t bss-portal-csr:rust .

# Newer than the backend services' 1.80: the portals pull in bss-branding's logo
# processing (image 0.25 → MSRV 1.88; moxcms 0.8 → edition 2024). Matches the dev
# toolchain (1.97).
# cargo-chef (Phase 8): chef → planner → cook compile the ~450-crate dep graph in a
# layer keyed on Cargo.toml/Cargo.lock, so a source-only edit rebuilds just the
# workspace crates + this binary (~2 min). The chef/planner/cook stages are
# byte-identical across BOTH portal Dockerfiles, so the cook layer is built once and
# shared. See rust/portals/self-serve/Dockerfile + services/catalog/Dockerfile.
FROM rust:1.97-slim AS chef
WORKDIR /build/rust
COPY rust/rust-toolchain.toml .
RUN rustup show >/dev/null && cargo install cargo-chef --locked

FROM chef AS planner
COPY rust ./
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
COPY --from=planner /build/rust/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY rust ./
RUN cargo build --release -p portal-csr

FROM gcr.io/distroless/cc-debian12 AS runtime
# distroless nonroot uid 65532.
USER 65532:65532
COPY --from=builder /build/rust/target/release/portal-csr /usr/local/bin/portal-csr
# Asset trees (env overrides — the CARGO_MANIFEST_DIR defaults don't exist here).
COPY portals/csr/bss_csr/static                     /assets/local/static
COPY portals/csr/bss_csr/templates                  /assets/local/templates
COPY packages/bss-portal-ui/bss_portal_ui/static    /assets/shared/static
COPY packages/bss-portal-ui/bss_portal_ui/templates /assets/shared/templates
ENV BSS_CSR_STATIC_DIR=/assets/local/static \
    BSS_CSR_TEMPLATE_DIR=/assets/local/templates \
    BSS_PORTAL_SHARED_STATIC_DIR=/assets/shared/static \
    BSS_PORTAL_SHARED_TEMPLATE_DIR=/assets/shared/templates \
    BSS_PORTAL_CSR_PORT=8000
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/portal-csr"]
