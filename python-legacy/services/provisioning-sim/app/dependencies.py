"""Dependencies — lifespan, session factory, DI providers."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from bss_middleware import validate_api_token_present
from bss_telemetry import configure_telemetry
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.esim_provider import select_esim_provider
from app.events.consumer import setup_consumer
from app.repositories.fault_repo import FaultRepository
from app.repositories.task_repo import TaskRepository
from app.services.provisioning_service import ProvisioningService

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="provisioning-sim", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.mq_exchange = None
    app.state.mq_connection = None
    app.state.esim_provider = select_esim_provider(settings.esim_provider)
    log.info("esim_provider.selected", name=settings.esim_provider)

    # Set up MQ consumer (non-blocking — logs warning if MQ not configured)
    try:
        await setup_consumer(app)
    except Exception:
        log.warning("mq.consumer.setup_failed", exc_info=True)

    log.info("service.starting", service=settings.service_name)
    yield
    log.info("service.stopping", service=settings.service_name)

    # Teardown MQ
    if app.state.mq_connection:
        try:
            await app.state.mq_connection.close()
        except Exception:
            log.warning("mq.connection.close_failed", exc_info=True)

    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_provisioning_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ProvisioningService:
    return ProvisioningService(
        session=session,
        task_repo=TaskRepository(session),
        fault_repo=FaultRepository(session),
        exchange=request.app.state.mq_exchange,
        esim_provider=request.app.state.esim_provider,
    )
