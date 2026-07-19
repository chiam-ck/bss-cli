"""Small helpers for ASCII-box rendering shared by the hero renderers."""

from __future__ import annotations


def state_dot(state: str) -> str:
    """Return a coloured state indicator string suitable for plain-text output.

    We don't use rich markup here because renderer output is also fed to the
    LLM — use unambiguous ASCII. The ``●`` plus the state name conveys state.
    """
    return f"● {state.upper()}"


def progress_bar(used: float, total: float | None, width: int = 26) -> str:
    """Render a [████▌░░░] progress bar with sub-character resolution.

    Block-element gradient (▏▎▍▌▋▊▉█) gives 8x sub-cell resolution, so
    17%-vs-19% bars look distinguishable instead of both rounding to ▏.
    ``total is None`` means unlimited (dash-filled).
    """
    if total is None or total <= 0:
        return "[" + "─" * width + "]"
    ratio = 0.0 if total == 0 else max(0.0, min(1.0, used / total))
    full_cells = int(ratio * width)
    fractional = (ratio * width) - full_cells
    # 8 partial-cell levels via Unicode block elements.
    partials = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉"]
    partial_idx = int(fractional * 8)
    bar = "█" * full_cells
    if partial_idx > 0 and full_cells < width:
        bar += partials[partial_idx]
        bar += "░" * (width - full_cells - 1)
    else:
        bar += "░" * (width - full_cells)
    return "[" + bar + "]"


def double_box(lines: list[str], *, title: str, width: int = 64) -> str:
    """Wrap ``lines`` in a double-ruled (╔ ═ ╗) frame — visually heavier
    than ``box``. Used to make ``state=blocked`` jump off the page."""
    inner = width - 2
    top = f"╔═ {title} " + "═" * max(0, inner - len(title) - 3) + "╗"
    bottom = "╚" + "═" * inner + "╝"
    body = []
    for raw in lines:
        if len(raw) > inner - 2:
            raw = raw[: inner - 2]
        body.append("║ " + raw.ljust(inner - 2) + " ║")
    return "\n".join([top, *body, bottom])


def box(lines: list[str], *, title: str, width: int = 62) -> str:
    """Wrap ``lines`` in a unicode ASCII box with ``title`` in the top border."""
    inner = width - 2
    top = f"┌─ {title} " + "─" * max(0, inner - len(title) - 3) + "┐"
    bottom = "└" + "─" * inner + "┘"
    body = []
    for raw in lines:
        # pad/truncate to fit
        if len(raw) > inner - 2:
            raw = raw[: inner - 2]
        body.append("│ " + raw.ljust(inner - 2) + " │")
    return "\n".join([top, *body, bottom])


def format_msisdn(msisdn: str) -> str:
    """Format an 8-digit MSISDN as 'XXXX XXXX'."""
    s = str(msisdn or "")
    if len(s) == 8:
        return f"{s[:4]} {s[4:]}"
    return s


def format_iccid(iccid: str) -> str:
    """Format an ICCID with spaces every 4 digits."""
    s = str(iccid or "")
    return " ".join(s[i : i + 4] for i in range(0, len(s), 4))
