"""Chat-bubble HTML renderers shared by the customer-chat surface (v0.12)
and the operator-cockpit chat thread (v0.13).

Both surfaces stream the same shape — assistant-bubble + tool-call pill —
through SSE. v0.12 originated these helpers in
``portals/self-serve/.../routes/chat.py``; v0.13 lifts them here so the
cockpit thread renders identically without copy-paste drift.

Doctrine: the LLM's output is hostile-by-default. We HTML-escape every
character first, then convert a whitelisted set of markdown tokens into
HTML. No raw-HTML pass-through; no link/image rendering — those would
invite XSS via crafted prompt-injection responses. Same shape on both
surfaces; same security boundary.

v0.13.1 — extended block-level support: headings (``#``..``####``),
numbered lists (``1.``), code fences (``` ``` ```), and pipe tables
(``| col | col |``). Catalog and balance summaries from the LLM
typically come back as tables; render them as real ``<table>`` rather
than literal pipe characters.

v0.20.1 — pipe-table rendering is opt-in via ``allow_tables``. The
v0.19 doctrine that suppressed pipe tables in prose stays the default
(tool results render through ``bss_cockpit.renderers``, not via
fabricated ``<table>``). Callers that know a renderer-less tool was
the source of the prose — most importantly ``knowledge.search``,
which has no ASCII renderer and whose answer IS the LLM's prose —
flip ``allow_tables=True`` so the operator sees a real table instead
of literal pipes. The decision lives at the call site so both the
operator cockpit and any future surface can apply the same rule
without re-implementing the table grammar.

Channel-markup stripping (``<channel|>`` / ``assistantfinal`` and
friends) is delegated to ``bss_cockpit.postprocess.strip_channel_markup``
so REPL and browser converge on one regex.
"""

from __future__ import annotations

import html as _html
import re as _re

from bss_cockpit.postprocess import (
    strip_channel_markup as _strip_channel_markup,
    strip_reasoning_leakage as _shared_strip_reasoning_leakage,
)

__all__ = [
    "render_assistant_bubble",
    "render_tool_pill",
    "render_chat_markdown",
    "strip_reasoning_leakage",
]


# ── Inline patterns ─────────────────────────────────────────────────


# v0.20.1 — reasoning-leakage regexes moved to ``bss_cockpit.postprocess``
# so the REPL can use the same helper without depending on this portal-
# side module. Imported above as ``_shared_strip_reasoning_leakage``.


def strip_reasoning_leakage(text: str) -> str:
    """Public alias of the internal helper, for callers that want to
    sanitize text before persisting it (so the conversation row
    doesn't carry the leaked reasoning either)."""
    return _strip_reasoning_leakage(text)


def _strip_reasoning_leakage(text: str) -> str:
    """Remove gemma-style reasoning leakage AND Harmony channel
    markup from the start of a reply. v0.20.1 — single source of
    truth lives in ``bss_cockpit.postprocess``; this wrapper chains
    both helpers so the chat_html caller's contract is unchanged.
    """
    if not text:
        return text
    return _shared_strip_reasoning_leakage(_strip_channel_markup(text))


_RE_BOLD = _re.compile(r"\*\*(?P<inner>[^*\n]+)\*\*")
_RE_ITALIC_AST = _re.compile(r"(?<!\*)\*(?P<inner>[^*\n]+)\*(?!\*)")
_RE_ITALIC_UND = _re.compile(r"(?<!\w)_(?P<inner>[^_\n]+)_(?!\w)")
_RE_CODE = _re.compile(r"`(?P<inner>[^`\n]+)`")

# Block-level patterns. Operating on already-HTML-escaped text.
_RE_LIST_ITEM = _re.compile(r"^\s*[\*\-]\s+(?P<body>.*)$")
_RE_OL_ITEM = _re.compile(r"^\s*\d+[.)]\s+(?P<body>.*)$")
_RE_HEADING = _re.compile(r"^(?P<hashes>#{1,4})\s+(?P<body>.+?)\s*#*\s*$")
_RE_CODE_FENCE = _re.compile(r"^\s*```")
# A pipe-table row: starts and ends with `|`, has ≥1 inner pipe.
_RE_TABLE_ROW = _re.compile(r"^\s*\|.+\|\s*$")
# Separator row (after the header): cells are `---` / `:---:` / `---:` / `:---`
_RE_TABLE_SEP_CELL = _re.compile(r"^\s*:?-{2,}:?\s*$")
# v0.19 — Rich/box-drawing ASCII panel detection. The REPL renderers
# emit Panel / Table layouts using U+250x box characters; when the LLM
# regurgitates that output verbatim, a proportional-font browser with
# collapsed whitespace destroys the alignment. Treat any contiguous run
# of lines that begins with a panel top (`┌`) or contains a panel side
# (`│` / `└` / `├`) as a literal `<pre>` block.
_BOX_CHARS = "─━│┃┌┐└┘├┤┬┴┼═║╔╗╚╝"
_RE_ASCII_PANEL_LINE = _re.compile(rf"[{_BOX_CHARS}]")


def _render_inline(line: str) -> str:
    """Apply inline markdown to a single, already-HTML-escaped line.

    Order matters: code spans first (they shadow ``*`` etc.); bold
    before italic so ``**`` doesn't get partially-eaten.
    """
    out = _RE_CODE.sub(lambda m: f"<code>{m.group('inner')}</code>", line)
    out = _RE_BOLD.sub(lambda m: f"<strong>{m.group('inner')}</strong>", out)
    out = _RE_ITALIC_AST.sub(lambda m: f"<em>{m.group('inner')}</em>", out)
    out = _RE_ITALIC_UND.sub(lambda m: f"<em>{m.group('inner')}</em>", out)
    return out


def _split_table_row(row: str) -> list[str]:
    """Parse a ``| a | b | c |`` row into ``["a", "b", "c"]``.

    Trims leading/trailing pipe + whitespace, splits on ``|``, applies
    inline markdown to each cell.
    """
    trimmed = row.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return [cell.strip() for cell in trimmed.split("|")]


def _is_table_separator(row: str) -> bool:
    """True if every cell of ``row`` is a markdown table separator (``---``)."""
    cells = _split_table_row(row)
    if not cells:
        return False
    return all(_RE_TABLE_SEP_CELL.match(c) for c in cells)


def render_chat_markdown(text: str, *, allow_tables: bool = False) -> str:
    """Block-level + inline markdown render for assistant chat output.

    HTML-escapes the whole text once up front, then walks lines grouping
    list runs, tables, code fences, and paragraphs. Returns a single
    HTML string. Embedded newlines DO appear in the output for tables /
    code fences — SSE consumers should strip them at the wire layer if
    they need a single-line ``data:`` field.

    v0.13.1 — strips gemma's reasoning leakage (``<think>...</think>``,
    leading ``thought\\n``) before rendering.

    v0.20.1 — ``allow_tables=True`` opts into pipe-table → ``<table>``
    rendering. Default stays False to preserve the v0.19 doctrine that
    pipe tables in prose are usually a hallucination. Callers that
    know the prose came from a renderer-less tool (``knowledge.*``)
    flip this on so handbook tables render as real tables.
    """
    cleaned = _strip_reasoning_leakage(text or "")
    escaped = _html.escape(cleaned)
    lines = escaped.split("\n")

    out: list[str] = []
    para: list[str] = []
    ul_items: list[str] = []
    ol_items: list[str] = []
    fence_buf: list[str] | None = None

    def _flush_ul() -> None:
        if ul_items:
            out.append(
                "<ul>"
                + "".join(f"<li>{_render_inline(it)}</li>" for it in ul_items)
                + "</ul>"
            )
            ul_items.clear()

    def _flush_ol() -> None:
        if ol_items:
            out.append(
                "<ol>"
                + "".join(f"<li>{_render_inline(it)}</li>" for it in ol_items)
                + "</ol>"
            )
            ol_items.clear()

    def _flush_para() -> None:
        if para:
            joined = "<br>".join(_render_inline(p) for p in para)
            out.append(f"<p>{joined}</p>")
            para.clear()

    def _flush_blocks() -> None:
        _flush_ul()
        _flush_ol()
        _flush_para()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # Code fence: collect until closing fence; emit a <pre><code>.
        if fence_buf is not None:
            if _RE_CODE_FENCE.match(line):
                # Closing fence.
                out.append(
                    "<pre><code>" + "\n".join(fence_buf) + "</code></pre>"
                )
                fence_buf = None
            else:
                fence_buf.append(line)
            i += 1
            continue
        if _RE_CODE_FENCE.match(line):
            _flush_blocks()
            fence_buf = []
            i += 1
            continue

        # ASCII panel (Rich/box-drawing). Collect contiguous lines that
        # contain box-drawing characters into one <pre> block so the
        # browser renders them in monospace with literal whitespace.
        if _RE_ASCII_PANEL_LINE.search(line):
            _flush_blocks()
            panel: list[str] = [line]
            j = i + 1
            while j < len(lines) and _RE_ASCII_PANEL_LINE.search(lines[j]):
                panel.append(lines[j].rstrip())
                j += 1
            out.append("<pre><code>" + "\n".join(panel) + "</code></pre>")
            i = j
            continue

        # v0.19+ / v0.20.1 — pipe-table grammar is opt-in via
        # ``allow_tables``. Default stays OFF: tool results render
        # through ``bss_cockpit.renderers`` as deterministic ASCII
        # inside <pre>; an LLM bubble fabricating a table is a
        # doctrine bug we want VISIBLE (literal pipes survive so the
        # operator can see the attempt). Renderer-less tools — most
        # importantly ``knowledge.*``, where the LLM's prose IS the
        # answer — set ``allow_tables=True`` at the call site so a
        # pipe-table relayed from the handbook renders as a real
        # ``<table>``. Suppression for renderer-backed-tool turns
        # already happens upstream via ``_suppress_tool_recap`` in
        # the cockpit route, so this branch only fires on prose that
        # legitimately survived that gate.
        if allow_tables and _RE_TABLE_ROW.match(line):
            # Look ahead: a pipe-table in markdown is a header row,
            # a separator row (---|---), then ≥1 body rows. If the
            # next line isn't a separator, treat as paragraph (the
            # branch below handles literal pipes).
            if i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
                _flush_blocks()
                header_cells = _split_table_row(line)
                body_rows: list[list[str]] = []
                j = i + 2
                while j < len(lines) and _RE_TABLE_ROW.match(lines[j]):
                    body_rows.append(_split_table_row(lines[j]))
                    j += 1
                thead = (
                    "<thead><tr>"
                    + "".join(
                        f"<th>{_render_inline(c)}</th>" for c in header_cells
                    )
                    + "</tr></thead>"
                )
                tbody = (
                    "<tbody>"
                    + "".join(
                        "<tr>"
                        + "".join(
                            f"<td>{_render_inline(c)}</td>" for c in row
                        )
                        + "</tr>"
                        for row in body_rows
                    )
                    + "</tbody>"
                )
                out.append(f"<table>{thead}{tbody}</table>")
                i = j
                continue

        # Heading.
        m_h = _RE_HEADING.match(line)
        if m_h:
            _flush_blocks()
            depth = len(m_h.group("hashes"))
            # Cap at <h6> as a sanity backstop, render starting at <h3>
            # so chat bubbles don't blow up to page-title size.
            tag = f"h{min(6, max(3, 2 + depth))}"
            out.append(
                f"<{tag}>{_render_inline(m_h.group('body'))}</{tag}>"
            )
            i += 1
            continue

        # Unordered list.
        m_ul = _RE_LIST_ITEM.match(line)
        if m_ul:
            _flush_para()
            _flush_ol()
            ul_items.append(m_ul.group("body"))
            i += 1
            continue

        # Ordered list.
        m_ol = _RE_OL_ITEM.match(line)
        if m_ol:
            _flush_para()
            _flush_ul()
            ol_items.append(m_ol.group("body"))
            i += 1
            continue

        # Blank line — paragraph / list break.
        if not line.strip():
            _flush_blocks()
            i += 1
            continue

        # Plain paragraph line.
        _flush_ul()
        _flush_ol()
        para.append(line)
        i += 1

    # Close any open buffer cleanly.
    if fence_buf is not None:
        out.append("<pre><code>" + "\n".join(fence_buf) + "</code></pre>")
    _flush_blocks()
    return "".join(out) or "&nbsp;"


def render_assistant_bubble(
    text: str, *, error: bool = False, allow_tables: bool = False
) -> str:
    """Full assistant reply as a chat bubble. Single-line HTML for SSE.

    Adds ``chat-bubble-error`` modifier when ``error=True`` so portal
    CSS can dim or red-tint a fallback / ownership-violation reply.

    ``allow_tables`` (v0.20.1) is forwarded to
    :func:`render_chat_markdown` so callers can opt into pipe-table
    rendering when the prose came from a renderer-less tool.

    SSE wire format requires a single-line ``data:`` field; tables and
    code fences embed real newlines in the rendered output for human
    readability. Strip them here so the SSE frame stays one line; the
    browser renders them identically (HTML doesn't care about newlines
    inside <table>/<pre>).
    """
    css = "chat-bubble chat-bubble-assistant"
    if error:
        css += " chat-bubble-error"
    rendered = render_chat_markdown(text, allow_tables=allow_tables).replace(
        "\n", ""
    )
    return f'<div class="{css}">{rendered}</div>'


def render_tool_pill(tool_name: str) -> str:
    """Inline pill announcing a tool call to the human reader.

    Renders identically on the customer chat surface and the operator
    cockpit thread. The tool name is HTML-escaped — even though
    registered tool names follow a known shape, the renderer treats
    inputs as untrusted.
    """
    return (
        '<div class="chat-tool-pill">'
        '<span class="chat-tool-icon">≈</span>'
        f'<span class="chat-tool-name">{_html.escape(tool_name)}</span>'
        "</div>"
    )
