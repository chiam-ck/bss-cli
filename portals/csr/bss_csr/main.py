"""FastAPI app factory for the operator cockpit (v0.13).

Per phases/V0_13_0.md the v0.5 CSR portal pattern is retired. The
browser is now a thin veneer over the Postgres-backed cockpit
``Conversation`` store; the canonical surface is the CLI REPL. No
login. ``actor`` for cockpit turns comes from
``.bss-cli/settings.toml`` via ``bss_cockpit.config.current()``.

- ``BSSApiTokenMiddleware`` is NOT on the portal's inbound HTTP. The
  cockpit runs single-operator-by-design behind a secure perimeter
  (CLAUDE.md anti-pattern, DECISIONS 2026-05-01). Outbound calls to
  BSS services carry the cockpit's named token via the
  v0.9 ``NamedTokenAuthProvider("operator_cockpit", ...)``.
- ``configure_telemetry(service_name="portal-csr")`` runs in lifespan
  so portal spans surface in ``bss trace``.
- The shared ``bss-portal-ui`` package owns the cockpit's static
  assets at ``/portal-ui/static/``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import os
import structlog
from bss_cockpit import ConversationStore, configure_store
from bss_orchestrator.autonomy import read_autonomy_mode
from bss_portal_ui import STATIC_DIR as SHARED_STATIC_DIR
from bss_telemetry import configure_telemetry
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = app.state.settings
    configure_telemetry(service_name="portal-csr", app=app)

    # v1.5 — read BSS_REPL_LLM_AUTONOMY once at boot, cache on app.state.
    # read_autonomy_mode() raises AutonomyMisconfigured on a bad value;
    # we let it propagate so the cockpit refuses to start rather than
    # silently defaulting (same fail-closed shape as the v0.9 named-
    # token sentinel rejection).
    app.state.autonomy_mode = read_autonomy_mode()

    # v0.13 — boot the cockpit Conversation store. Both surfaces
    # (the CLI REPL and this browser veneer) share the singleton via
    # the bss_cockpit.configure_store() default-store registry.
    db_url = os.environ.get("BSS_DB_URL", "")
    if not db_url:
        raise RuntimeError(
            "BSS_DB_URL is unset; the operator cockpit cannot boot "
            "without its Conversation store."
        )
    store = ConversationStore(db_url=db_url)
    configure_store(store)
    app.state.cockpit_store = store

    log.info(
        "cockpit.starting",
        service=settings.service_name,
        version=settings.version,
        autonomy_mode=app.state.autonomy_mode,
    )
    try:
        yield
    finally:
        log.info("cockpit.stopping", service=settings.service_name)
        await store.dispose()
        configure_store(None)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    app = FastAPI(
        title=f"BSS-CLI {settings.service_name}",
        version=settings.version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount(
        "/portal-ui/static",
        StaticFiles(directory=SHARED_STATIC_DIR),
        name="portal-ui-static",
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": settings.service_name,
                "version": settings.version,
            }
        )

    from .routes import case, cockpit, search, settings as settings_routes

    app.include_router(cockpit.router)
    app.include_router(case.router)
    app.include_router(search.router)
    app.include_router(settings_routes.router)

    return app


app = create_app
