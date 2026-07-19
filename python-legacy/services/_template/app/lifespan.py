from contextlib import asynccontextmanager

import structlog
from bss_telemetry import configure_telemetry
from fastapi import FastAPI

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Replace "<service-name>" with the actual service name (e.g. "crm", "com")
    configure_telemetry(service_name="<service-name>", app=app)
    log.info("service.starting")
    yield
    log.info("service.stopping")
