"""CRM service — FastAPI app factory."""

from bss_clock import clock_admin_router
from bss_events import audit_events_router
from bss_middleware import BSSApiTokenMiddleware
from fastapi import FastAPI

from app.api import admin, health
from app.api.crm import agent, case, chat_transcript, kyc, port_request
from app.api.inventory import esim, msisdn
from app.api.tmf import customer, interaction, ticket
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

    # Order matters — last added is outermost. Auth runs FIRST so
    # unauth'd requests fail before any contextvar / log work.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(BSSApiTokenMiddleware)

    # Health
    app.include_router(health.router)

    # TMF629 Customer Management
    app.include_router(
        customer.router,
        prefix="/tmf-api/customerManagement/v4",
    )

    # TMF683 Customer Interaction Management
    app.include_router(
        interaction.router,
        prefix="/tmf-api/customerInteractionManagement/v1",
    )

    # TMF621 Trouble Ticket
    app.include_router(
        ticket.router,
        prefix="/tmf-api/troubleTicket/v4",
    )

    # Custom CRM — Case
    app.include_router(case.router, prefix="/crm-api/v1")

    # Custom CRM — KYC
    app.include_router(kyc.router, prefix="/crm-api/v1")

    # Custom CRM — Agent
    app.include_router(agent.router, prefix="/crm-api/v1")

    # Custom CRM — Chat transcripts (v0.12)
    app.include_router(chat_transcript.router, prefix="/crm-api/v1")

    # Custom CRM — Port requests (v0.17 MNP, operator-only)
    app.include_router(port_request.router, prefix="/crm-api/v1")

    # Inventory — MSISDN
    app.include_router(msisdn.router, prefix="/inventory-api/v1")

    # Inventory — eSIM
    app.include_router(esim.router, prefix="/inventory-api/v1")

    # Admin — operational-data reset (gated by BSS_ALLOW_ADMIN_RESET)
    app.include_router(admin.router, prefix="/admin-api/v1")

    # Admin — scenario clock control (gated by BSS_ALLOW_ADMIN_RESET)
    app.include_router(clock_admin_router(), prefix="/admin-api/v1")

    # Audit — read-only view onto audit.domain_event
    app.include_router(audit_events_router(), prefix="/audit-api/v1")

    return app
