"""v1.5 — chrome filter for cockpit-message history rehydration.

When ``Conversation.transcript_text()`` rehydrates the cockpit's prior
turns into the LLM's context for ``astream_once(transcript=...)``, the
LLM should see only genuine user / assistant / tool turns — NOT the
cockpit's own emit chrome (the ``(no reply)`` fallback bubble, the
``(The model called X but did not synthesise a final answer.)``
Gemma-recovery bubble, the citation-guard fallback, the route
"something went wrong" error bubble).

If chrome is left in, the LLM sees its own past placeholder output and
mistakes it for prior reasoning, which produces three failure modes
(observed in loyalty-cli pre-filter and in bss-cli pre-v1.5 long
conversations):

1. **Mimicry.** The LLM emits new turns in the same chrome shape
   instead of doing the work — "(no reply)" becomes a regular output.
2. **Confusion about state.** The "did not synthesise" bubble reads as
   "I tried and failed last turn", which biases the model toward
   re-trying the same broken call instead of investigating.
3. **Citation thrash.** Seeing "I don't have a citation for that" in
   history teaches the model to short-circuit to that fallback even
   when knowledge.search would have succeeded this turn.

This module is the runtime backstop for those three. The prompt
doctrine in ``bss_cockpit.prompts._COCKPIT_INVARIANTS`` is the first
line of defence; this filter catches the cases where the doctrine
fails.

Patterns matched are exact prefixes of the cockpit's own emit
strings — so a legitimate LLM reply that happens to contain the word
"reply" or "synthesise" in prose is NOT filtered. The detector is
conservative: false negatives just leave benign noise; false
positives would strip real LLM output.

Lifted from loyalty-cli's ``_is_cockpit_chrome`` /
``_strip_fake_propose`` pattern (``cli/loyalty-cli/src/loyalty_cli/
repl/loop.py``). The bss-cli chrome strings are different (different
cockpit, different fallback wording) but the design is identical.
"""

from __future__ import annotations

import re
from typing import Final

# Exact prefixes of every chrome-shaped assistant string the cockpit
# emits in v1.4.1. Any addition to the cockpit's emit set (a new
# fallback bubble, a new error panel) MUST also land here — the unit
# tests pin the inventory so the omission shows up in CI.
_ASSISTANT_CHROME_PREFIXES: Final[tuple[str, ...]] = (
    # Route-level error fallback (portals/csr cockpit.py).
    "Sorry — something went wrong",
    # Gemma empty-final-after-tool-calls recovery (portals/csr + cli REPL).
    "(The model called ",
    # Total empty-AIMessage fallback (same two sites).
    "(no reply)",
    # Citation guard fallback (_KNOWLEDGE_HALLUCINATION_FALLBACK).
    "I don't have a citation for that",
)


# v1.5 — LLMs sometimes emit text that LOOKS LIKE the cockpit's
# propose banner (anti-mimicry rule in the system prompt is the first
# line of defence; this regex is the runtime backstop). Matches a
# line-leading ``⚠ PROPOSE: ...`` or ``PROPOSE: ...`` shape so the
# stripped reply still reads cleanly.
_FAKE_PROPOSE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:⚠\s*)?PROPOSE\s*(?:\[\s*step\s*\d+\s*\])?\s*:.*?(?:\n|$)",
    re.MULTILINE | re.IGNORECASE,
)


def is_cockpit_chrome(content: str) -> bool:
    """True when a persisted assistant message is cockpit-rendered chrome
    rather than something the LLM actually said.

    Empty / whitespace-only content is treated as chrome (the cockpit
    never persists empty real replies — they get replaced with the
    ``(no reply)`` fallback before persistence).
    """
    if not content or not content.strip():
        return True
    return any(content.startswith(p) for p in _ASSISTANT_CHROME_PREFIXES)


def strip_fake_propose(text: str) -> tuple[str, bool]:
    """Strip cockpit-banner-shaped lines from an LLM text reply.

    Returns ``(cleaned_text, was_modified)``. The legitimate prose
    (the ask, the explanation, the wrap-up) is preserved; only the
    chrome-shaped lines are removed. Operators reading the cleaned
    output don't see a misleading PROPOSE-shape that won't fire.
    """
    cleaned, n = _FAKE_PROPOSE_LINE_RE.subn("", text)
    cleaned = cleaned.strip()
    return cleaned, n > 0
