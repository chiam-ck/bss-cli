from bss_clock import clock_admin_router
from bss_middleware import BSSApiTokenMiddleware
from fastapi import FastAPI

from bss_catalog.config import Settings
from bss_catalog.deps import lifespan
from bss_catalog.logging import configure_logging
from bss_catalog.middleware import RequestIdMiddleware
from bss_catalog.routes import (
    admin,
    health,
    product_offering,
    product_offering_price,
    product_specification,
    promotion,
    vas,
)


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

    app.include_router(health.router)
    app.include_router(
        product_offering.router,
        prefix="/tmf-api/productCatalogManagement/v4",
    )
    app.include_router(
        product_offering_price.router,
        prefix="/tmf-api/productCatalogManagement/v4",
    )
    app.include_router(
        product_specification.router,
        prefix="/tmf-api/productCatalogManagement/v4",
    )
    app.include_router(vas.router, prefix="/vas")
    app.include_router(admin.router)
    # v1.1 — promotion routes carry absolute paths (TMF671 + portal-facing reads).
    app.include_router(promotion.router)
    # v0.7 — catalog now reads the scenario clock for active-window resolution.
    app.include_router(clock_admin_router(), prefix="/admin-api/v1")

    return app
