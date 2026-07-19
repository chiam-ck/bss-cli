FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir uv

# v0.6: workspace-aware build via uv 0.11+ (UV_PROJECT_ENVIRONMENT).
# Replaces the v0.1 sed workaround — see DECISIONS.md 2026-04-23.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/provisioning-sim/ services/provisioning-sim/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync --package provisioning-sim --frozen --no-dev

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
CMD ["python", "-m", "app"]
