"""SOM (Service Order Management) — FastAPI app factory."""

from bss_clock import clock_admin_router
from bss_events import audit_events_router
from bss_middleware import BSSApiTokenMiddleware
from fastapi import FastAPI

from app.api import admin, health, service, service_order
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

    # TMF641 — ServiceOrder
    app.include_router(
        service_order.router,
        prefix="/tmf-api/serviceOrderingManagement/v4",
    )

    # TMF638 — Service Inventory
    app.include_router(
        service.router,
        prefix="/tmf-api/serviceInventoryManagement/v4",
    )

    # Admin — operational-data reset (gated by BSS_ALLOW_ADMIN_RESET)
    app.include_router(admin.router, prefix="/admin-api/v1")

    # Admin — scenario clock control (gated by BSS_ALLOW_ADMIN_RESET)
    app.include_router(clock_admin_router(), prefix="/admin-api/v1")

    # Audit — read-only view onto audit.domain_event
    app.include_router(audit_events_router(), prefix="/audit-api/v1")

    return app
