"""`bss branding ...` — operator branding from the terminal (v1.8).

Thin verbs over the same seams the cockpit Branding screen uses:
reads via ``bss_branding`` (file values, no env overrides baked in),
writes via ``bss_cockpit.config.write_branding_settings`` — the
validation gate. No business logic here (doctrine: CLI handlers call
the config layer, nothing more). The logo image upload stays on the
cockpit screen; the primary visual editor is
http://localhost:9002/settings/branding.
"""

from __future__ import annotations

from typing import Annotated

import bss_branding
import typer
from bss_branding import LOGO_MARKS, THEMES
from bss_cockpit.config import write_branding_settings
from pydantic import ValidationError
from rich import print as rprint
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    help="Operator branding — name, theme, logo mark. Visual editor: cockpit → Branding.",
    no_args_is_help=True,
)


def _swatch(theme_id: str) -> Text:
    t = THEMES[theme_id]
    row = Text()
    for color in (t.accent, t.fg, t.bg_elev, t.accent_amber):
        row.append("██", style=color)
        row.append(" ")
    return row


def _save(**fields: str) -> None:
    saved = bss_branding.file_settings()
    try:
        write_branding_settings(saved.model_copy(update=fields))
    except (ValidationError, ValueError) as exc:
        rprint(f"[red]rejected:[/] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("show")
def show() -> None:
    """Current branding (as resolved, env overrides applied)."""
    view = bss_branding.current()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("operator name", f"[bold]{view.brand_name}[/]")
    table.add_row("theme", f"{view.theme.id}  [dim]({view.theme.label})[/]")
    table.add_row("", _swatch(view.theme.id))
    table.add_row("mark", f"[{view.theme.rich_accent}]{view.mark}[/]")
    table.add_row(
        "logo image",
        str(view.logo_path) if view.logo_path else "[dim](none — headers use the mark)[/]",
    )
    rprint(table)


@app.command("themes")
def themes() -> None:
    """List the available theme palettes."""
    active = bss_branding.current().theme.id
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("id")
    table.add_column("label")
    table.add_column("palette")
    for t in THEMES.values():
        marker = " [green]← active[/]" if t.id == active else ""
        table.add_row(t.id, t.label + marker, _swatch(t.id))
    rprint(table)


@app.command("set-theme")
def set_theme(
    theme_id: Annotated[str, typer.Argument(help="One of: " + ", ".join(THEMES))],
) -> None:
    """Switch the color scheme (portals, emails, REPL banner)."""
    if theme_id not in THEMES:
        rprint(f"[red]unknown theme[/] {theme_id!r} — pick one of: {', '.join(THEMES)}")
        raise typer.Exit(code=1)
    _save(theme=theme_id)
    rprint(f"theme → [bold]{theme_id}[/]  ", _swatch(theme_id))


@app.command("set-name")
def set_name(
    name: Annotated[str, typer.Argument(help="Operator brand name (1-40 chars)")],
) -> None:
    """Set the operator name shown in headers, emails, chat and banner."""
    _save(brand_name=name)
    rprint(f"operator name → [bold]{name}[/]")


@app.command("set-mark")
def set_mark(
    mark: Annotated[str, typer.Argument(help=f"1-3 chars; built-ins: {' '.join(LOGO_MARKS)}")],
) -> None:
    """Set the text logo mark (used everywhere an image can't render)."""
    _save(mark=mark)
    rprint(f"mark → [bold]{mark}[/]")
