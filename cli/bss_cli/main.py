"""`bss` — root Typer app. Direct commands + `bss ask` + `bss` REPL."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from bss_telemetry import configure_telemetry
from rich import print as rprint


def _bootstrap_env_from_dotenv() -> None:
    """Load `<repo>/.env` into ``os.environ`` if not already exported.

    The cockpit REPL (v0.13+) needs ``BSS_DB_URL`` and
    ``BSS_OPERATOR_COCKPIT_API_TOKEN`` available as process env — both
    are read directly via ``os.environ`` (the cockpit's
    ``ConversationStore`` and ``astream_once``'s
    ``_resolve_token_for_service_identity`` resp.). ``uv run bss``
    doesn't source ``.env`` automatically; pre-v0.13 the REPL was
    in-memory and got away without DB credentials, so the gap never
    surfaced. Auto-loading here keeps the operator workflow simple:
    ``uv run bss`` works out of the box.

    Existing env vars take precedence — exported values from a parent
    shell or compose file are not overwritten.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        # Strip optional surrounding quotes — common in .env files.
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_bootstrap_env_from_dotenv()

from .commands import (
    admin as admin_cmd,
    case as case_cmd,
    catalog as catalog_cmd,
    clock as clock_cmd,
    customer as customer_cmd,
    external_calls as external_calls_cmd,
    inventory as inventory_cmd,
    onboard as onboard_cmd,
    order as order_cmd,
    payment as payment_cmd,
    promo as promo_cmd,
    prov as prov_cmd,
    scenario as scenario_cmd,
    som as som_cmd,
    subscription as subscription_cmd,
    ticket as ticket_cmd,
    trace as trace_cmd,
    usage as usage_cmd,
)

app = typer.Typer(
    name="bss",
    help="BSS-CLI — terminal-first, LLM-native telco BSS.",
    no_args_is_help=False,
)

# Direct command groups.
app.add_typer(customer_cmd.app, name="customer")
app.add_typer(case_cmd.app, name="case")
app.add_typer(ticket_cmd.app, name="ticket")
app.add_typer(order_cmd.app, name="order")
app.add_typer(subscription_cmd.app, name="subscription")
app.add_typer(catalog_cmd.app, name="catalog")
app.add_typer(payment_cmd.app, name="payment")
app.add_typer(promo_cmd.app, name="promo")
app.add_typer(usage_cmd.app, name="usage")
app.add_typer(prov_cmd.app, name="prov")
app.add_typer(som_cmd.app, name="som")
app.add_typer(inventory_cmd.app, name="inventory")
app.add_typer(clock_cmd.app, name="clock")
app.add_typer(trace_cmd.app, name="trace")
app.add_typer(admin_cmd.app, name="admin")
app.add_typer(scenario_cmd.app, name="scenario")
app.add_typer(onboard_cmd.app, name="onboard")
app.add_typer(external_calls_cmd.app, name="external-calls")


# `bss ask "..."` is a top-level command, not a subgroup.
@app.command("ask")
def ask_cmd(
    prompt: Annotated[str, typer.Argument(help="Natural-language request.")],
    allow_destructive: Annotated[
        bool, typer.Option("--allow-destructive", help="Permit destructive tool calls.")
    ] = False,
) -> None:
    """Single-shot LLM dispatch. Use the REPL (just `bss`) for multi-turn."""
    from .llm_runner import run_single_shot

    run_single_shot(prompt, allow_destructive=allow_destructive)


# `bss` with no subcommand → REPL (canonical CSR cockpit, v0.13).
@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Resume a specific cockpit session id (SES-...).",
        ),
    ] = None,
    new: Annotated[
        bool,
        typer.Option(
            "--new",
            help="Force a fresh cockpit session even if one is active.",
        ),
    ] = False,
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Optional human-readable label for the new session.",
        ),
    ] = None,
    list_sessions: Annotated[
        bool,
        typer.Option(
            "--list",
            help=(
                "Print operator's recent cockpit sessions and exit "
                "(non-interactive)."
            ),
        ),
    ] = False,
    allow_destructive: Annotated[
        bool, typer.Option("--allow-destructive")
    ] = False,
) -> None:
    # CLI is the root span for every `bss <cmd>` invocation. Without
    # this, traces would start at the first BSS service that the CLI
    # touches, hiding the CLI's own latency and breaking the
    # `audit.domain_event.trace_id` story for CLI-originated writes.
    configure_telemetry(service_name="cli")
    if ctx.invoked_subcommand is not None:
        return
    try:
        from .repl import run_repl
    except ImportError as e:
        rprint(f"[red]REPL unavailable: {e}[/]")
        raise typer.Exit(code=1)
    run_repl(
        allow_destructive=allow_destructive,
        session_id=session,
        force_new=new,
        label=label,
        list_only=list_sessions,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
