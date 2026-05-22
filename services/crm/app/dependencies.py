from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bss_clients import BearerAuthProvider, LoyaltyClient, TokenAuthProvider
from bss_clients.subscription import SubscriptionClient
from bss_middleware import api_token, validate_api_token_present
from bss_telemetry import configure_telemetry

from app.repositories.case_repo import CaseRepository
from app.repositories.chat_transcript_repo import ChatTranscriptRepository
from app.repositories.customer_repo import CustomerRepository
from app.repositories.esim_repo import EsimRepository
from app.repositories.interaction_repo import InteractionRepository
from app.repositories.kyc_repo import KycRepository
from app.repositories.msisdn_repo import MsisdnRepository
from app.repositories.port_request_repo import PortRequestRepository
from app.repositories.ticket_repo import TicketRepository
from app.services.case_service import CaseService
from app.services.chat_transcript_service import ChatTranscriptService
from app.services.customer_service import CustomerService
from app.services.inventory_service import InventoryService
from app.services.kyc_service import KycService
from app.services.port_request_service import PortRequestService
from app.services.ticket_service import TicketService

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_token_present()  # fail-fast before any other setup
    settings = app.state.settings
    configure_telemetry(service_name="crm", app=app)
    engine = create_async_engine(settings.db_url, pool_size=5, max_overflow=5)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.subscription_client = SubscriptionClient(
        base_url=settings.subscription_url,
        auth_provider=TokenAuthProvider(api_token()),
    )
    # v1.1.1 — loyalty client for customer-registry sync. OPTIONAL: when unset,
    # the sync is simply skipped (CustomerService guards on a None client).
    if settings.loyalty_api_token:
        app.state.loyalty_client = LoyaltyClient(
            base_url=settings.loyalty_base_url,
            auth_provider=BearerAuthProvider(settings.loyalty_api_token),
        )
    else:
        app.state.loyalty_client = None
        log.warning("crm.loyalty.disabled", reason="BSS_LOYALTY_API_TOKEN unset")
    log.info("service.starting", service=settings.service_name)
    yield
    await app.state.subscription_client.close()
    if app.state.loyalty_client is not None:
        await app.state.loyalty_client.close()
    log.info("service.stopping", service=settings.service_name)
    await engine.dispose()


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


# ── Repositories ────────────────────────────────────────────────────

async def get_customer_repo(
    session: AsyncSession = Depends(get_session),
) -> CustomerRepository:
    return CustomerRepository(session)


async def get_case_repo(
    session: AsyncSession = Depends(get_session),
) -> CaseRepository:
    return CaseRepository(session)


async def get_ticket_repo(
    session: AsyncSession = Depends(get_session),
) -> TicketRepository:
    return TicketRepository(session)


async def get_interaction_repo(
    session: AsyncSession = Depends(get_session),
) -> InteractionRepository:
    return InteractionRepository(session)


async def get_kyc_repo(
    session: AsyncSession = Depends(get_session),
) -> KycRepository:
    return KycRepository(session)


async def get_msisdn_repo(
    session: AsyncSession = Depends(get_session),
) -> MsisdnRepository:
    return MsisdnRepository(session)


async def get_esim_repo(
    session: AsyncSession = Depends(get_session),
) -> EsimRepository:
    return EsimRepository(session)


# ── Services ────────────────────────────────────────────────────────

async def get_customer_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CustomerService:
    return CustomerService(
        session=session,
        customer_repo=CustomerRepository(session),
        interaction_repo=InteractionRepository(session),
        subscription_client=request.app.state.subscription_client,
        msisdn_repo=MsisdnRepository(session),
        loyalty_client=request.app.state.loyalty_client,
    )


async def get_kyc_service(
    session: AsyncSession = Depends(get_session),
) -> KycService:
    return KycService(
        session=session,
        customer_repo=CustomerRepository(session),
        kyc_repo=KycRepository(session),
        interaction_repo=InteractionRepository(session),
    )


async def get_case_service(
    session: AsyncSession = Depends(get_session),
) -> CaseService:
    return CaseService(
        session=session,
        case_repo=CaseRepository(session),
        customer_repo=CustomerRepository(session),
        ticket_repo=TicketRepository(session),
        interaction_repo=InteractionRepository(session),
    )


async def get_chat_transcript_service(
    session: AsyncSession = Depends(get_session),
) -> ChatTranscriptService:
    return ChatTranscriptService(
        session=session,
        chat_transcript_repo=ChatTranscriptRepository(session),
    )


async def get_ticket_service(
    session: AsyncSession = Depends(get_session),
) -> TicketService:
    return TicketService(
        session=session,
        ticket_repo=TicketRepository(session),
        customer_repo=CustomerRepository(session),
        case_repo=CaseRepository(session),
        interaction_repo=InteractionRepository(session),
    )


async def get_inventory_service(
    session: AsyncSession = Depends(get_session),
) -> InventoryService:
    return InventoryService(
        session=session,
        msisdn_repo=MsisdnRepository(session),
        esim_repo=EsimRepository(session),
    )


async def get_port_request_repo(
    session: AsyncSession = Depends(get_session),
) -> PortRequestRepository:
    return PortRequestRepository(session)


async def get_port_request_service(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PortRequestService:
    return PortRequestService(
        session=session,
        port_repo=PortRequestRepository(session),
        msisdn_repo=MsisdnRepository(session),
        subscription_client=request.app.state.subscription_client,
    )
