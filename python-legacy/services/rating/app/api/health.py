from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    return {"status": "ok", "service": settings.service_name, "version": settings.version}


@router.get("/ready")
async def ready(request: Request) -> dict:
    settings = request.app.state.settings
    engine = request.app.state.engine
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready", "service": settings.service_name}
    except Exception:
        return {"status": "unavailable", "service": settings.service_name}
