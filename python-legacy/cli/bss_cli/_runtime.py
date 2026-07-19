"""Small async→sync runtime helper for Typer commands.

Typer commands are synchronous, but bss-clients is async. Every command uses
``run_async`` to execute a coroutine, set CLI channel context, and close
the shared client bundle at exit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from bss_orchestrator.clients import close_clients
from bss_orchestrator.context import use_cli_context

T = TypeVar("T")


def run_async(coro: Awaitable[T]) -> T:
    """Set CLI channel context, run the coroutine, then close clients."""
    async def _main() -> T:
        use_cli_context()
        try:
            return await coro
        finally:
            await close_clients()

    return asyncio.run(_main())
