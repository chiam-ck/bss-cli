"""Single-shot LLM dispatch — powers ``bss ask "..."``.

Lives in the CLI package (not the orchestrator) because it deals with Rich
rendering + Typer error reporting. Orchestrator stays UI-free.
"""

from __future__ import annotations

import asyncio

import typer
from bss_orchestrator.clients import close_clients
from bss_orchestrator.session import ask_once
from rich import print as rprint
from rich.panel import Panel


def run_single_shot(prompt: str, *, allow_destructive: bool = False) -> None:
    """Ask the LLM once, print the reply, exit. No session state retained."""

    async def _go() -> str:
        try:
            return await ask_once(prompt, allow_destructive=allow_destructive)
        finally:
            await close_clients()

    try:
        reply = asyncio.run(_go())
    except RuntimeError as e:
        # Most common cause: missing BSS_LLM_API_KEY. Surface it cleanly
        # instead of dumping a stack into the user's terminal.
        rprint(f"[red]LLM unavailable:[/] {e}")
        raise typer.Exit(code=1) from e

    if not reply.strip():
        rprint("[yellow](no reply)[/]")
        return

    rprint(Panel(reply, title="bss ai", border_style="green"))
