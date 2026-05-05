"""Shared post-processing of LLM final-message text before display.

v0.20.1 — model output is hostile-by-default and now leaks two new
artefacts that the v0.13 / v0.19 renderers don't handle:

* ``<channel|>`` / ``<|channel|>`` / ``assistantfinal`` — Harmony /
  channel-format control tokens that some open-weight models (gemma,
  gpt-oss) emit into the final-message stream when their response
  format isn't enforced server-side. The browser surface escapes the
  bytes so the operator sees the literal ``<channel|>`` text;
  the REPL surface dumps it raw inside a Rich Panel. Both are ugly.

* Pipe-table markdown carrying knowledge-search content. v0.19's
  ``chat_html`` deliberately refuses to render ``| col | col |`` as a
  real ``<table>`` because tool results are supposed to come through
  ``bss_cockpit.renderers``. v0.20 carved out ``knowledge.*`` from the
  anti-recap rule: when ``knowledge.search`` fires, the LLM's prose
  IS the answer, and tables in that prose are legitimate. The
  ``knowledge_called()`` helper here is the seam consumers use to
  decide whether the table-grammar gate should open.

Both helpers are surface-agnostic. The REPL imports them from here;
the browser cockpit imports them from here; future surfaces (Slack,
operator API) import them from here.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

__all__ = [
    "knowledge_called",
    "strip_channel_markup",
    "strip_reasoning_leakage",
]


# Harmony / channel-format leakage shapes seen in the wild from gemma
# and gpt-oss derivatives. We strip:
#   ``<channel|>``     — pipe-suffix variant (most common)
#   ``<|channel|>``    — fully-bracketed variant
#   ``</channel>``     — close tag
#   ``<channel>``      — open tag (rare; sibling of the close)
#   ``assistantfinal`` — bare bare-word marker on its own line / start
# All strips are case-insensitive and the close-side variants survive
# multi-line text because the regex doesn't anchor lines.
_RE_CHANNEL_MARKUP = re.compile(
    r"(?:<\|?channel\|?>|</\s*channel\s*>|^\s*assistantfinal\s*\n?)",
    re.IGNORECASE | re.MULTILINE,
)


def strip_channel_markup(text: str) -> str:
    """Remove Harmony / channel-format control tokens from LLM output.

    Idempotent. Leading whitespace introduced by the strip is trimmed
    so the rendered bubble doesn't open with a blank line. Trailing
    whitespace is preserved (some renderers care about paragraph
    boundaries at end-of-text).
    """
    if not text:
        return text
    cleaned = _RE_CHANNEL_MARKUP.sub("", text)
    return cleaned.lstrip()


# v0.13.1 — gemma occasionally leaks reasoning-step tokens into the
# regular content channel. Three shapes seen in the wild:
#
#   ``<think>...</think>\nAnswer.``         — XML-style block
#   ``thought\n\nAnswer.``                   — bare "thought" header
#   ``thought Answer.``                      — bare "thought" prefix on
#                                              the same line (v0.20.1)
#
# All three are stripped at the boundary so neither display nor
# persistence carries the leakage. v0.20.1 lifted the helper out of
# ``bss_portal_ui.chat_html`` into this shared module so the REPL can
# use it without a portal-side import.
_RE_THINK_BLOCK = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL
)
_RE_LEADING_THOUGHT = re.compile(
    r"^\s*(?:thought|thinking)\s*[:\-]?\s*\n+", re.IGNORECASE
)
# Same-line prefix variant: ``thought Searched for ...``. The trailing
# whitespace is required so the regex doesn't eat valid words like
# "thoughtful". Only fires at start-of-text.
_RE_INLINE_THOUGHT_PREFIX = re.compile(
    r"^\s*(?:thought|thinking)\s+(?=\S)", re.IGNORECASE
)


def strip_reasoning_leakage(text: str) -> str:
    """Remove gemma-style reasoning leakage that surfaces in the
    regular content channel.

    Three shapes seen in the wild — ``<think>...</think>`` blocks,
    leading ``thought\\n\\n``, and same-line ``thought ...`` prefixes.
    Idempotent; safe to chain with ``strip_channel_markup``.
    """
    if not text:
        return text
    cleaned = _RE_THINK_BLOCK.sub("", text)
    cleaned = _RE_LEADING_THOUGHT.sub("", cleaned, count=1)
    cleaned = _RE_INLINE_THOUGHT_PREFIX.sub("", cleaned, count=1)
    return cleaned.lstrip()


def knowledge_called(
    captured_tool_calls: Iterable[Mapping[str, object]] | Sequence[object] | None,
) -> bool:
    """Return True iff any ``knowledge.*`` tool fired this turn.

    Accepts the cockpit's ``captured_tool_calls`` shape — a list of
    ``{"name": "...", "args": {...}}`` mappings — but tolerates other
    iterables (e.g. plain lists of names) for callers that don't keep
    args around. The tool-recap doctrine carve-out for ``knowledge.*``
    (v0.20) is the gate this helper is most often used to open: when
    a knowledge-search tool fires, the LLM's prose is the authoritative
    answer (no ASCII renderer exists for ``knowledge.*``), so table
    grammar inside that prose should render rather than fall through
    as literal pipes.
    """
    if not captured_tool_calls:
        return False
    for entry in captured_tool_calls:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, Mapping):
            raw = entry.get("name")
            if not isinstance(raw, str):
                continue
            name = raw
        else:
            continue
        if name.startswith("knowledge."):
            return True
    return False
