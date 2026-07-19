from fastapi import FastAPI

from app.config import settings
from app.lifespan import lifespan
from app.logging import configure_logging

configure_logging(settings.log_level)

app = FastAPI(title=settings.service_name, lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": settings.service_name}
