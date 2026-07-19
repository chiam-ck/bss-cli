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
# cargo-chef (Phase 8): chef → planner → cook compile the ~450-crate dep graph in a
# layer keyed on Cargo.toml/Cargo.lock, so a source-only edit rebuilds just the
# workspace crates + this binary (~2 min). The chef/planner/cook stages are
# byte-identical across BOTH portal Dockerfiles, so the cook layer is built once and
# shared. (Services use the same pattern on the rust:1.80 base + context=rust/;
# the portals keep their repo-root context + 1.97 base, so they form a second,
# separate cook layer — see services/catalog/Dockerfile for the rationale.)
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
RUN cargo build --release -p portal-self-serve

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
# Distroless has no curl; the binary self-probes GET /health via --healthcheck
# (mirrors the Python image's `curl -f http://localhost:8000/health || exit 1`).
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["/usr/local/bin/portal-self-serve", "--healthcheck"]
ENTRYPOINT ["/usr/local/bin/portal-self-serve"]
