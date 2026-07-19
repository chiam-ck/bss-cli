"""Provisioning simulator — FastAPI app factory."""

from bss_clock import clock_admin_router
from bss_events import audit_events_router
from bss_middleware import BSSApiTokenMiddleware
from fastapi import FastAPI

from app.api import admin, fault_injection, health, task
from app.config import Settings
from app.dependencies import lifespan
from app.logging import configure_logging
from app.middleware import RequestIdMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=f"BSS-CLI {settings.service_name}",
        version=settings.version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(BSSApiTokenMiddleware)  # last added = outermost; auth runs first

    # Health
    app.include_router(health.router)

    # Provisioning API
    app.include_router(
        task.router,
        prefix="/provisioning-api/v1",
    )
    app.include_router(
        fault_injection.router,
        prefix="/provisioning-api/v1",
    )

    # Admin — operational-data reset (gated by BSS_ALLOW_ADMIN_RESET)
    app.include_router(admin.router, prefix="/admin-api/v1")

    # Admin — scenario clock control (gated by BSS_ALLOW_ADMIN_RESET)
    app.include_router(clock_admin_router(), prefix="/admin-api/v1")

    # Audit — read-only view onto audit.domain_event
    app.include_router(audit_events_router(), prefix="/audit-api/v1")

    return app
