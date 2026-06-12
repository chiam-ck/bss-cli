"""`bss ask "..."` and REPL-less `bss` entrypoint — single-shot LLM dispatch.

Phase 9 wiring: construct the LangGraph agent, send one user message, stream
the result back. Uses OpenRouter via openai SDK (no LiteLLM).
"""

from __future__ import annotations

from typing import Annotated

import typer

app = typer.Typer(help="Natural-language LLM dispatch.", no_args_is_help=True)


@app.command("ask")
def ask(
    prompt: Annotated[str, typer.Argument(help="Natural-language request.")],
    allow_destructive: Annotated[
        bool, typer.Option("--allow-destructive", help="Permit destructive tool calls.")
    ] = False,
) -> None:
    """Single-shot: ask the LLM, run the resulting tool calls, print the result."""
    # Imported lazily so `bss --help` doesn't pay the LangGraph import cost.
    from ..llm_runner import run_single_shot

    run_single_shot(prompt, allow_destructive=allow_destructive)
