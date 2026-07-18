# Rust CSR/cockpit portal — multi-stage, distroless final. Build context = REPO ROOT
# (see the self-serve Dockerfile header): the portal serves Jinja templates + static
# assets from disk, reused verbatim from the Python source tree.
#
#   docker build -f rust/portals/csr/Dockerfile -t bss-portal-csr:rust .

# Newer than the backend services' 1.80: the portals pull in bss-branding's logo
# processing (image 0.25 → MSRV 1.88; moxcms 0.8 → edition 2024). Matches the dev
# toolchain (1.97).
FROM rust:1.97-slim AS builder
WORKDIR /build
COPY rust ./rust
RUN cargo build --release --manifest-path rust/Cargo.toml -p portal-csr

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
