"""Dependencies — lifespan, session factory, DI providers."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from bss_clients import InventoryClient, TokenAuthProvider
from bss_events import start_relay
from bss_middleware import api_token, validate_api_token_present
from bss_telemetry import configure_telemetry
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.events.consumer import setup_consumer
from app.repositories.service_order_repo import ServiceOrderRepository
from app.repositories.service_repo import ServiceRepository
from app.services.som_service import SOMService

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="som", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.mq_exchange = None
    app.state.mq_connection = None

    # Inventory client (for MSISDN + eSIM reservation)
    inventory_client = InventoryClient(
        base_url=settings.crm_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    app.state.inventory_client = inventory_client

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
            interval_ms=getattr(settings, "outbox_relay_interval_ms", 250),
            batch_size=getattr(settings, "outbox_relay_batch_size", 100),
        )
    except Exception:
        log.warning("outbox.relay.setup_failed", exc_info=True)

    log.info("service.starting", service=settings.service_name)
    yield
    log.info("service.stopping", service=settings.service_name)

    # Teardown
    if app.state.outbox_relay is not None:
        await app.state.outbox_relay.stop()
    if app.state.mq_connection:
        try:
            await app.state.mq_connection.close()
        except Exception:
            log.warning("mq.connection.close_failed", exc_info=True)

    await inventory_client.close()
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_som_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SOMService:
    return SOMService(
        session=session,
        so_repo=ServiceOrderRepository(session),
        svc_repo=ServiceRepository(session),
        inventory_client=request.app.state.inventory_client,
        exchange=request.app.state.mq_exchange,
    )
