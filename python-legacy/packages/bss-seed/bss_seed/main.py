"""Seed runner — populates reference data across all domains.

Idempotent: safe to re-run. Uses INSERT ... ON CONFLICT DO NOTHING.
"""

import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from . import catalog, crm, inventory, provisioning


async def run_seed() -> None:
    url = os.environ.get("BSS_DB_URL")
    if not url:
        print("ERROR: BSS_DB_URL environment variable is not set", file=sys.stderr)
        sys.exit(1)

    engine = create_async_engine(url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        async with session.begin():
            await catalog.seed(session)
            await inventory.seed(session)
            await crm.seed(session)
            await provisioning.seed(session)

    await engine.dispose()
    print("Seed complete.")


def main() -> None:
    asyncio.run(run_seed())


if __name__ == "__main__":
    main()
