# Rust self-serve portal — multi-stage, distroless final. Unlike the backend service
# images (context = rust/), the build context here is the REPO ROOT so the Dockerfile
# can copy both the rust workspace (to build) and the Jinja templates + static assets
# the portal serves from disk. Those assets live in the Python source tree
# (portals/self-serve/..., packages/bss-portal-ui/...) and are reused verbatim by the
# Rust port — the templating/ServeDir loaders read them at runtime.
#
#   docker build -f rust/portals/self-serve/Dockerfile -t bss-portal-self-serve:rust .

# Newer than the backend services' 1.80: the portals pull in bss-branding's logo
# processing (image 0.25 → MSRV 1.88; moxcms 0.8 → edition 2024), which the services
# don't. Matches the workspace dev toolchain (1.97).
FROM rust:1.97-slim AS builder
WORKDIR /build
COPY rust ./rust
RUN cargo build --release --manifest-path rust/Cargo.toml -p portal-self-serve

FROM gcr.io/distroless/cc-debian12 AS runtime
# distroless nonroot uid 65532.
USER 65532:65532
COPY --from=builder /build/rust/target/release/portal-self-serve /usr/local/bin/portal-self-serve
# The default template/static paths resolve against CARGO_MANIFEST_DIR, which doesn't
# exist in the runtime image, so copy the asset trees in and point the env overrides
# at them (minijinja loader + tower-http ServeDir read these at request time).
COPY portals/self-serve/bss_self_serve/static       /assets/local/static
COPY portals/self-serve/bss_self_serve/templates    /assets/local/templates
COPY packages/bss-portal-ui/bss_portal_ui/static    /assets/shared/static
COPY packages/bss-portal-ui/bss_portal_ui/templates /assets/shared/templates
ENV BSS_PORTAL_STATIC_DIR=/assets/local/static \
    BSS_PORTAL_TEMPLATE_DIR=/assets/local/templates \
    BSS_PORTAL_SHARED_STATIC_DIR=/assets/shared/static \
    BSS_PORTAL_SHARED_TEMPLATE_DIR=/assets/shared/templates \
    BSS_PORTAL_SELF_SERVE_PORT=8000
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/portal-self-serve"]
