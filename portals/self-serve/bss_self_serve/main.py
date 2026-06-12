"""FastAPI app factory for the self-serve portal.

Per V0_4_0.md §8 / V0_8_0.md §2.2:

- ``BSSApiTokenMiddleware`` is NOT on the portal's inbound HTTP. The
  portal is a customer-facing surface protected by the v0.8 portal-auth
  layer (PortalSessionMiddleware below). Outbound calls carry the
  portal's named token via ``bss_self_serve.clients.get_clients()``
  (v0.9+: ``BSS_PORTAL_SELF_SERVE_API_TOKEN`` → ``service_identity = "portal_self_serve"``
  on the receiving side; falls back to ``BSS_API_TOKEN`` if the named
  token is not provisioned).
- ``configure_telemetry(service_name="portal-self-serve")`` runs in
  lifespan so portal spans show up in ``bss trace``.
- v0.8 lifespan adds:
    * ``validate_pepper_present()`` — fail fast if BSS_PORTAL_TOKEN_PEPPER
      is unset / sentinel / too short.
    * ``app.state.db_engine`` + ``app.state.db_session_factory`` — async
      engine reading ``BSS_DB_URL``. The session middleware and security
      deps both pull the factory off ``app.state``.
    * ``app.state.email_adapter`` — selected via
      ``BSS_PORTAL_EMAIL_PROVIDER`` env (logging | noop | resend | smtp-stub).
      ``BSS_PORTAL_EMAIL_ADAPTER`` (old name) is read as a deprecated
      fallback until v0.16.
    * ``PortalSessionMiddleware`` is mounted on the FastAPI app so it
      runs on every request before route resolution.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from bss_portal_auth import (
    Settings as PortalAuthSettings,
)
from bss_portal_auth import (
    select_adapter,
    validate_pepper_present,
)
from bss_portal_auth.email import resolve_provider_name
from bss_portal_ui import STATIC_DIR as SHARED_STATIC_DIR
from bss_telemetry import configure_telemetry
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .chat_session import ChatConversationStore, ChatTurnStore
from .config import Settings
from .kyc import select_kyc_adapter
from .middleware import PortalSessionMiddleware
from .security import install_redirect_handlers
from .session import SessionStore

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = app.state.settings
    # Portal telemetry — same bootstrap as the 9 services. No
    # validate_api_token_present() call because the portal itself
    # doesn't require the token inbound; outbound calls read it lazily
    # when constructing bss-clients via the orchestrator factory.
    configure_telemetry(service_name="portal-self-serve", app=app)

    # v0.8 — portal-auth startup checks BEFORE any auth flow can run.
    validate_pepper_present()

    # v0.8 — DB engine for portal_auth identity / session storage.
    # Optional: if BSS_DB_URL is unset (e.g. unit-test app construction
    # without a DB), middleware fails open (logs once and passes through).
    portal_auth_settings = PortalAuthSettings()
    # v0.14 — webhook route reads BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET
    # off this; not loading per-request (env reads are doctrine-prohibited).
    app.state.portal_auth_settings = portal_auth_settings
    if settings.bss_db_url:
        engine = create_async_engine(settings.bss_db_url, pool_size=5, max_overflow=5)
        app.state.db_engine = engine
        app.state.db_session_factory = async_sessionmaker(
            engine, expire_on_commit=False
        )
    else:
        app.state.db_engine = None
        app.state.db_session_factory = None
        log.warning("portal.db_url.missing")

    # v0.14 — email adapter selection. LoggingEmailAdapter writes to a
    # file the operator can `tail -f`; NoopEmailAdapter is for tests;
    # ResendEmailAdapter is the v0.14 production adapter; 'smtp' raises
    # at construction (reserved post-v0.16).
    #
    # v0.14 renamed BSS_PORTAL_EMAIL_ADAPTER → BSS_PORTAL_EMAIL_PROVIDER.
    # `resolve_provider_name` accepts either, warns on the old name,
    # and is removed when the alias is dropped in v0.16.
    provider_name = resolve_provider_name(
        provider=portal_auth_settings.BSS_PORTAL_EMAIL_PROVIDER,
        legacy_adapter=portal_auth_settings.BSS_PORTAL_EMAIL_ADAPTER,
    )
    app.state.email_adapter = select_adapter(
        provider_name,
        portal_auth_settings.BSS_PORTAL_DEV_MAILBOX_PATH,
        resend_api_key=portal_auth_settings.BSS_PORTAL_EMAIL_RESEND_API_KEY,
        from_address=portal_auth_settings.BSS_PORTAL_EMAIL_FROM,
    )

    # v0.15 — KYC adapter selection. Prebaked is deterministic dev/scenario
    # path; Didit is the real provider. Selection is fail-fast on missing
    # creds. Trust anchor for Didit attestations is the HMAC-signed webhook
    # recorded in integrations.kyc_webhook_corroboration; the adapter blocks
    # on that row before returning.
    app.state.kyc_adapter = select_kyc_adapter(
        name=settings.bss_portal_kyc_provider,
        didit_api_key=settings.bss_portal_kyc_didit_api_key,
        didit_workflow_id=settings.bss_portal_kyc_didit_workflow_id,
        session_factory=app.state.db_session_factory,
    )
    log.info(
        "portal.kyc_adapter.selected",
        provider=settings.bss_portal_kyc_provider,
    )

    # v0.16 — payment provider mode + PCI scope guard.
    # The mode drives signup template selection (mock = server-rendered
    # card-number form; stripe = redirect to Stripe Checkout).
    # The PCI guard refuses to boot in production-stripe mode if any
    # template still has a `name="card_number"` input — the doctrine
    # line is "PAN never touches BSS in production" (DECISIONS 2026-05-03).
    app.state.payment_provider = settings.bss_payment_provider
    app.state.payment_stripe_api_key = settings.bss_payment_stripe_api_key
    if (
        settings.bss_env == "production"
        and settings.bss_payment_provider == "stripe"
    ):
        from .pci_scope import scan_templates_for_pan_inputs

        scan_templates_for_pan_inputs()  # raises RuntimeError if any survive

        if not settings.bss_payment_stripe_api_key:
            raise RuntimeError(
                "BSS_PAYMENT_PROVIDER=stripe in production requires "
                "BSS_PAYMENT_STRIPE_API_KEY (the portal calls "
                "stripe.checkout.Session.create server-side; the "
                "secret key must be present)."
            )
    log.info(
        "portal.payment_provider.selected",
        provider=settings.bss_payment_provider,
    )

    app.state.session_store = SessionStore(
        ttl_seconds=settings.bss_portal_self_serve_session_ttl,
    )

    # v0.12 PR7/PR13 — chat in-memory state.
    #   * ChatConversationStore: durable per-customer history so a
    #     second message in the same conversation lands with full
    #     prior-turn context. 60 min idle TTL.
    #   * ChatTurnStore: per-SSE-stream working set for the current
    #     in-flight turn. 30 min TTL covers a customer who Tab-
    #     switched between submitting and opening the stream.
    app.state.chat_turn_store = ChatTurnStore(ttl_seconds=1800)
    app.state.chat_conversation_store = ChatConversationStore(ttl_seconds=3600)
    log.info(
        "portal.starting",
        service=settings.service_name,
        email_provider=provider_name,
        db_url_set=bool(settings.bss_db_url),
    )
    yield
    log.info("portal.stopping", service=settings.service_name)
    if app.state.db_engine is not None:
        await app.state.db_engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    app = FastAPI(
        title=f"BSS-CLI {settings.service_name}",
        version=settings.version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # v0.8 — session middleware runs on every request. Pure ASGI so SSE
    # streams (/agent/events/{session_id}) keep working. Mounted via
    # add_middleware so it sits OUTSIDE the FastAPI router.
    app.add_middleware(PortalSessionMiddleware)

    # 303 redirect handlers for the gating dependencies in security.py.
    install_redirect_handlers(app)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    # Shared UI assets (vendored HTMX + base CSS) live in bss-portal-ui.
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

    from .routes import (
        activation,
        auth,
        billing,
        cancel,
        chat,
        confirmation,
        esim,
        landing,
        legal,
        msisdn_picker,
        payment_methods,
        plan_change,
        profile,
        session_api,
        signup,
        top_up,
        webhooks,
        welcome,
    )

    app.include_router(auth.router)
    app.include_router(welcome.router)
    # v0.12 PR20 — public legal pages.
    app.include_router(legal.router)
    app.include_router(landing.router)
    app.include_router(msisdn_picker.router)
    app.include_router(signup.router)
    app.include_router(activation.router)
    app.include_router(confirmation.router)
    app.include_router(session_api.router)
    # v0.10 — post-login self-serve writes go direct (no orchestrator).
    app.include_router(top_up.router)
    app.include_router(payment_methods.router)
    app.include_router(esim.router)
    app.include_router(cancel.router)
    app.include_router(profile.router)
    app.include_router(billing.router)
    app.include_router(plan_change.router)
    # v0.12 — chat surface, the only orchestrator-mediated route.
    app.include_router(chat.router)
    # v0.14 — inbound provider webhooks (Resend; v0.15 adds Didit).
    # Exempt from BSSApiTokenMiddleware via WEBHOOK_EXEMPT_PATHS;
    # signature verification happens inside the route handler.
    app.include_router(webhooks.router)

    return app


# Uvicorn entry point: ``portal-self-serve = bss_self_serve.main:app``
# but since we need create_app(), the Dockerfile calls with --factory.
app = create_app
