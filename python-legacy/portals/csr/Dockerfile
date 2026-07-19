FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir uv

# v0.6: workspace-aware build via uv 0.11+ (UV_PROJECT_ENVIRONMENT).
# Replaces the v0.4 sed workaround — see DECISIONS.md 2026-04-23.
# Portals depend on bss-orchestrator, so orchestrator/ is in the
# build context too.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY orchestrator/ orchestrator/
COPY portals/csr/ portals/csr/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync --package portal-csr --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN useradd -m -u 1000 bss && \
    apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder --chown=bss:bss /app/.venv /app/.venv
COPY --from=builder --chown=bss:bss /build /build
WORKDIR /build
USER bss
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "bss_csr.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
