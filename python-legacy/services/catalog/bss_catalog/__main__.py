import uvicorn

from bss_catalog.config import Settings

settings = Settings()

uvicorn.run(
    "bss_catalog.app:create_app",
    factory=True,
    host="0.0.0.0",
    port=8000,
    log_level=settings.log_level.lower(),
)
