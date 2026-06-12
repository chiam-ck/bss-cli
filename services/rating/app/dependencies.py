"""Dependencies — lifespan, session factory, DI providers."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from bss_clients import CatalogClient, TokenAuthProvider
from bss_middleware import api_token, validate_api_token_present
from bss_telemetry import configure_telemetry
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.events.consumer import setup_consumer

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="rating", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.mq_exchange = None
    app.state.mq_connection = None

    app.state.catalog_client = CatalogClient(
        base_url=settings.catalog_url,
        auth_provider=TokenAuthProvider(api_token()),
    )

    try:
        await setup_consumer(app)
    except Exception:
        log.warning("mq.consumer.setup_failed", exc_info=True)

    log.info("service.starting", service=settings.service_name)
    yield
    log.info("service.stopping", service=settings.service_name)

    if app.state.mq_connection:
        try:
            await app.state.mq_connection.close()
        except Exception:
            log.warning("mq.connection.close_failed", exc_info=True)

    await app.state.catalog_client.close()
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_catalog_client(request: Request) -> CatalogClient:
    return request.app.state.catalog_client
