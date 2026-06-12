"""Dependencies — lifespan, session factory, DI providers."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aio_pika
import structlog
from bss_clients import SubscriptionClient, TokenAuthProvider
from bss_middleware import api_token, validate_api_token_present
from bss_telemetry import configure_telemetry
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repositories.usage_repo import UsageEventRepository
from app.services.mediation_service import MediationService

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="mediation", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.mq_exchange = None
    app.state.mq_connection = None

    app.state.subscription_client = SubscriptionClient(
        base_url=settings.subscription_url,
        auth_provider=TokenAuthProvider(api_token()),
    )

    # MQ — best-effort: mediation publishes usage.recorded / usage.rejected.
    if settings.mq_url:
        try:
            connection = await aio_pika.connect_robust(settings.mq_url)
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                "bss.events",
                aio_pika.ExchangeType.TOPIC,
                durable=True,
            )
            app.state.mq_connection = connection
            app.state.mq_exchange = exchange
            log.info("mq.connected", exchange="bss.events")
        except Exception:
            log.warning("mq.connect.failed", exc_info=True)
    else:
        log.warning("mq.not_configured")

    log.info("service.starting", service=settings.service_name)
    yield
    log.info("service.stopping", service=settings.service_name)

    if app.state.mq_connection:
        try:
            await app.state.mq_connection.close()
        except Exception:
            log.warning("mq.connection.close_failed", exc_info=True)

    await app.state.subscription_client.close()
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_mediation_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MediationService:
    return MediationService(
        session=session,
        repo=UsageEventRepository(session),
        subscription_client=request.app.state.subscription_client,
        exchange=request.app.state.mq_exchange,
    )
