"""Dependencies — lifespan, session factory, DI providers."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from bss_clients import (
    BearerAuthProvider,
    CatalogClient,
    CRMClient,
    LoyaltyClient,
    NoAuthProvider,
    PaymentClient,
    SOMClient,
    SubscriptionClient,
    TokenAuthProvider,
)
from bss_events import start_relay
from bss_middleware import api_token, validate_api_token_present
from bss_telemetry import configure_telemetry
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.events.consumer import setup_consumer
from app.repositories.order_repo import OrderRepository
from app.services.order_service import OrderService
from app.workers.reconciliation import reconciliation_tick_loop

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="com", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.mq_exchange = None
    app.state.mq_connection = None

    # Service-to-service clients
    crm_client = CRMClient(
        base_url=settings.crm_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    catalog_client = CatalogClient(
        base_url=settings.catalog_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    payment_client = PaymentClient(
        base_url=settings.payment_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    som_client = SOMClient(
        base_url=settings.som_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    subscription_client = SubscriptionClient(
        base_url=settings.subscription_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    # v1.1 — COM's own loyalty client (consume lifecycle). OPTIONAL: when the
    # token is unset, loyalty is OFF and the order/activation flow runs normally
    # without promos (no discount stamped → nothing to claim). Never blocks COM.
    if settings.loyalty_api_token:
        loyalty_client = LoyaltyClient(
            base_url=settings.loyalty_base_url,
            auth_provider=BearerAuthProvider(settings.loyalty_api_token),
        )
    else:
        loyalty_client = None
        log.warning("com.loyalty.disabled", reason="BSS_LOYALTY_API_TOKEN unset")

    app.state.crm_client = crm_client
    app.state.catalog_client = catalog_client
    app.state.payment_client = payment_client
    app.state.som_client = som_client
    app.state.subscription_client = subscription_client
    app.state.loyalty_client = loyalty_client

    # Set up MQ consumer (non-blocking — logs warning if MQ not configured)
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
            interval_ms=settings.outbox_relay_interval_ms,
            batch_size=settings.outbox_relay_batch_size,
        )
    except Exception:
        log.warning("outbox.relay.setup_failed", exc_info=True)

    # v1.2 — reconciliation sweeper (stuck-order backstop). 0 disables (tests).
    app.state.reconciliation_task = None
    if settings.reconciliation_interval_seconds > 0:
        app.state.reconciliation_task = asyncio.create_task(
            reconciliation_tick_loop(app, settings.reconciliation_interval_seconds)
        )

    log.info("service.starting", service=settings.service_name)
    yield
    log.info("service.stopping", service=settings.service_name)

    # Teardown
    if app.state.reconciliation_task is not None:
        app.state.reconciliation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.reconciliation_task
    if app.state.outbox_relay is not None:
        await app.state.outbox_relay.stop()
    if app.state.mq_connection:
        try:
            await app.state.mq_connection.close()
        except Exception:
            log.warning("mq.connection.close_failed", exc_info=True)

    await crm_client.close()
    await catalog_client.close()
    await payment_client.close()
    await som_client.close()
    await subscription_client.close()
    if loyalty_client is not None:
        await loyalty_client.close()
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_order_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OrderService:
    return OrderService(
        session=session,
        repo=OrderRepository(session),
        crm_client=request.app.state.crm_client,
        catalog_client=request.app.state.catalog_client,
        payment_client=request.app.state.payment_client,
        som_client=request.app.state.som_client,
        subscription_client=request.app.state.subscription_client,
        loyalty_client=request.app.state.loyalty_client,
        exchange=request.app.state.mq_exchange,
    )
