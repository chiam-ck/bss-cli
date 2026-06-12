from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from bss_clients import CRMClient, TokenAuthProvider
from bss_middleware import api_token, validate_api_token_present
from bss_telemetry import configure_telemetry
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.select_tokenizer import select_tokenizer
from app.repositories.payment_attempt_repo import PaymentAttemptRepository
from app.repositories.payment_method_repo import PaymentMethodRepository
from app.services.payment_method_service import PaymentMethodService
from app.services.payment_service import PaymentService

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast on misconfig
    settings = app.state.settings
    configure_telemetry(service_name="payment", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.crm_client = CRMClient(
        base_url=settings.crm_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    # v0.16: TokenizerAdapter selected once at startup; fail-fast on
    # any misconfig (unknown provider, missing creds, sk_test_* in
    # production, ALLOW_TEST_CARD_REUSE + sk_live_*). Service refuses
    # to start rather than silently downgrading.
    app.state.tokenizer = select_tokenizer(
        name=settings.payment_provider,
        env=settings.env,
        stripe_api_key=settings.payment_stripe_api_key,
        stripe_publishable_key=settings.payment_stripe_publishable_key,
        stripe_webhook_secret=settings.payment_stripe_webhook_secret,
        allow_test_card_reuse=settings.payment_allow_test_card_reuse,
        session_factory=app.state.session_factory,
    )
    log.info(
        "service.starting",
        service=settings.service_name,
        payment_provider=settings.payment_provider,
    )
    yield
    log.info("service.stopping", service=settings.service_name)
    await app.state.crm_client.close()
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


# ── Repositories ────────────────────────────────────────────────────


async def get_pm_repo(
    session: AsyncSession = Depends(get_session),
) -> PaymentMethodRepository:
    return PaymentMethodRepository(session)


async def get_attempt_repo(
    session: AsyncSession = Depends(get_session),
) -> PaymentAttemptRepository:
    return PaymentAttemptRepository(session)


# ── Services ────────────────────────────────────────────────────────


async def get_payment_method_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PaymentMethodService:
    return PaymentMethodService(
        session=session,
        pm_repo=PaymentMethodRepository(session),
        crm_client=request.app.state.crm_client,
        tokenizer=getattr(request.app.state, "tokenizer", None),
    )


async def get_payment_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PaymentService:
    return PaymentService(
        session=session,
        attempt_repo=PaymentAttemptRepository(session),
        pm_repo=PaymentMethodRepository(session),
        tokenizer=request.app.state.tokenizer,
    )
