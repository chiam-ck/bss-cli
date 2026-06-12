import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from bss_clients import (
    CatalogClient,
    CRMClient,
    InventoryClient,
    PaymentClient,
    TokenAuthProvider,
)
from bss_events import start_relay
from bss_middleware import api_token, validate_api_token_present
from bss_portal_auth.email import select_adapter as select_email_adapter
from bss_telemetry import configure_telemetry
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.events.consumer import setup_consumer
from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.vas_repo import VasPurchaseRepository
from app.services.subscription_service import SubscriptionService
from app.workers.renewal import _renewal_tick_loop

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="subscription", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.mq_exchange = None
    app.state.mq_connection = None
    app.state.crm_client = CRMClient(
        base_url=settings.crm_url, auth_provider=TokenAuthProvider(api_token())
    )
    app.state.payment_client = PaymentClient(
        base_url=settings.payment_url, auth_provider=TokenAuthProvider(api_token())
    )
    app.state.catalog_client = CatalogClient(
        base_url=settings.catalog_url, auth_provider=TokenAuthProvider(api_token())
    )
    app.state.inventory_client = InventoryClient(
        base_url=settings.crm_url, auth_provider=TokenAuthProvider(api_token())
    )

    try:
        await setup_consumer(app)
    except Exception:
        log.warning("mq.consumer.setup_failed", exc_info=True)

    # v1.2 — outbox relay: the single publisher. Drains staged audit rows to MQ.
    app.state.outbox_relay = None
    try:
        app.state.outbox_relay = await start_relay(
            mq_url=settings.mq_url,
            session_factory=app.state.session_factory,
            interval_ms=getattr(settings, "outbox_relay_interval_ms", 250),
            batch_size=getattr(settings, "outbox_relay_batch_size", 100),
        )
    except Exception:
        log.warning("outbox.relay.setup_failed", exc_info=True)

    # v0.18 — email adapter for the upcoming-renewal reminder. Re-uses
    # the same env vars the portal-self-serve already reads
    # (BSS_PORTAL_EMAIL_PROVIDER + RESEND_API_KEY + EMAIL_FROM), so a
    # production deploy already has this configured. Set provider to
    # "noop" to disable the reminder sweep silently in tests; set
    # BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS=0 to disable on a running
    # service without changing email provider.
    try:
        app.state.email_adapter = select_email_adapter(
            os.environ.get("BSS_PORTAL_EMAIL_PROVIDER", "logging"),
            os.environ.get(
                "BSS_PORTAL_DEV_MAILBOX_PATH",
                "/tmp/bss-portal-mailbox.log",
            ),
            resend_api_key=os.environ.get(
                "BSS_PORTAL_EMAIL_RESEND_API_KEY", ""
            ),
            from_address=os.environ.get("BSS_PORTAL_EMAIL_FROM", ""),
        )
    except Exception:
        log.warning("renewal.reminder.email_adapter_init_failed", exc_info=True)
        app.state.email_adapter = None

    # v0.18 — automated renewal worker (in-process tick loop). Disable
    # by setting BSS_RENEWAL_TICK_SECONDS=0 (useful for tests that
    # don't want a background ticker firing during their assertions,
    # or when an external scheduler drives /admin-api/v1/renewal/tick-now).
    tick = int(os.environ.get("BSS_RENEWAL_TICK_SECONDS", "60"))
    if tick > 0:
        app.state.renewal_task = asyncio.create_task(
            _renewal_tick_loop(app, tick)
        )
    else:
        app.state.renewal_task = None
        log.info(
            "renewal.worker.disabled",
            reason="BSS_RENEWAL_TICK_SECONDS=0",
        )

    log.info("service.starting", service=settings.service_name)
    yield
    log.info("service.stopping", service=settings.service_name)

    # v0.18 — cancel renewal worker BEFORE closing payment/catalog
    # clients so an in-flight renew() doesn't hit a closed httpx client.
    if app.state.renewal_task is not None:
        app.state.renewal_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.renewal_task

    if app.state.outbox_relay is not None:
        await app.state.outbox_relay.stop()

    if app.state.mq_connection:
        try:
            await app.state.mq_connection.close()
        except Exception:
            log.warning("mq.connection.close_failed", exc_info=True)

    await app.state.crm_client.close()
    await app.state.payment_client.close()
    await app.state.catalog_client.close()
    await app.state.inventory_client.close()
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_subscription_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SubscriptionService:
    return SubscriptionService(
        session=session,
        repo=SubscriptionRepository(session),
        vas_repo=VasPurchaseRepository(session),
        crm_client=request.app.state.crm_client,
        payment_client=request.app.state.payment_client,
        catalog_client=request.app.state.catalog_client,
        inventory_client=request.app.state.inventory_client,
    )
