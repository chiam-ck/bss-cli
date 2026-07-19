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
# line of defence; this regex is the runtime backstop). Two shapes
# observed in the wild:
#
# A. Banner mimicry — leading ``⚠ PROPOSE:`` or ``PROPOSE:``:
#       ⚠ PROPOSE: subscription.terminate subscription_id='SUB-0001'
#       PROPOSE [step 2]: order.cancel order_id='ORD-007'
#
# B. Narrated-call mimicry — function-call shape in prose, often
#    paired with ``Please type /confirm``:
#       "I propose to terminate subscription SUB-0005.
#        subscription.terminate(subscription_id='SUB-0005')
#        Please type /confirm to proceed."
#
# Both must be stripped so the operator doesn't read banner-shaped
# prose that won't actually trigger anything. Shape B is harder to
# match cleanly because the model wraps it in arbitrary prose — we
# match the ``tool.name(args)`` chunk + any "type /confirm" boilerplate
# that surrounds it.
_FAKE_PROPOSE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:⚠\s*)?PROPOSE\s*(?:\[\s*step\s*\d+\s*\])?\s*:.*?(?:\n|$)",
    re.MULTILINE | re.IGNORECASE,
)

# Shape B detector. ``<lower_words>.<lower_words>(...)`` is the
# function-call shape; we require at least one dot so we don't match
# arbitrary parenthesised prose. Greedy to the closing paren on the
# same line. Consumes the optional surrounding backticks (`call(...)`
# / ``call(...)``) the LLM often wraps around the call shape — left
# behind those produce ugly empty-backtick fragments in the bubble.
_NARRATED_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"`{1,3}[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\([^)\n]*\)`{1,3}"
    r"|\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\([^)\n]*\)",
)

# Empty inline-code fragments left over when the strip removes a
# call from inside backticks but the backticks survived (the regex
# above handles backtick-wrapped calls; this is the safety-net for
# any stray pair). Two-backtick form (``) is the inline-code empty.
_EMPTY_BACKTICK_RE: Final[re.Pattern[str]] = re.compile(r"`{2,3}\s*`*")

# Mimicry narration that often surrounds the call shape — "I propose
# to ..." / "I'll call ..." / "I would like to ..." / "I'm going to
# ..." sentences. Removed when adjacent to a stripped call so the
# bubble doesn't read like a half-finished propose. Conservative:
# matches the leading clause only when the verb is one of a small
# canon (propose / call / terminate / cancel / close / remove / refund
# / revoke) — same canon that triggers the destructive contract.
_NARRATION_LEAD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|(?<=[.!?]\s))\s*"
    r"I(?:'ll|\s+will|\s+would\s+like|\s+intend|\s+propose|'m\s+going)"
    r"\s+to\s+"
    r"(?:propose|call|invoke|terminate|cancel|close|remove|refund|revoke)\b"
    r"[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)

# "Please type /confirm" boilerplate the model wraps around its
# narrated call. Matched as standalone sentences so we can strip them
# without slicing prose that LEGITIMATELY mentions /confirm (e.g. the
# anti-mimicry prompt rule itself, or a knowledge.search-grounded
# explanation).
_PLEASE_CONFIRM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\n)\s*(?:please\s+)?type\s+`?/confirm`?[^\n.]*\.?\s*",
    re.IGNORECASE,
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
    """Strip cockpit-banner-shaped lines AND narrated function-call
    shapes from an LLM text reply.

    Returns ``(cleaned_text, was_modified)``. The legitimate prose
    (the ask, the explanation, the wrap-up) is preserved; only the
    chrome-shaped fragments are removed. Operators reading the cleaned
    output don't see a misleading PROPOSE-shape or a fake
    "type /confirm" prompt that won't actually fire anything.

    Two strip passes:
      1. Banner mimicry (``⚠ PROPOSE: ...`` / ``PROPOSE: ...``) →
         delete the line.
      2. Narrated-call mimicry (``tool.name(args)`` in prose, often
         with surrounding "I propose ..." / "Please type /confirm ..."
         boilerplate) → delete the call shape AND the "/confirm"
         prompt sentence around it.

    Pass-2 is conservative: a single ``tool.name(arg)`` in legitimate
    prose (e.g. "I'm about to call `customer.get` — confirm CUST-001
    is the right id") is RARE; the cost of a false positive is
    explaining a missing fragment to the operator, vs. the cost of a
    false negative which is a stalled /confirm loop. The trade-off
    favours stripping.
    """
    cleaned, n_banner = _FAKE_PROPOSE_LINE_RE.subn("", text)
    cleaned, n_calls = _NARRATED_CALL_RE.subn("", cleaned)
    # If we stripped a call, also strip surrounding "I propose to ..."
    # narration AND any leftover empty backticks that the call was
    # wrapped in — both turn the bubble into half-finished propose
    # prose that would still mislead the operator.
    if n_calls > 0:
        cleaned, _ = _NARRATION_LEAD_RE.subn("", cleaned)
        cleaned, _ = _EMPTY_BACKTICK_RE.subn("", cleaned)
    cleaned, n_confirm = _PLEASE_CONFIRM_RE.subn(" ", cleaned)
    # Collapse runs of whitespace left behind by the inline strips,
    # but preserve paragraph breaks.
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    cleaned = cleaned.strip()
    # Only flag "narrated call" as a modification if the strip actually
    # removed a function-call shape — the /confirm-sentence regex on
    # its own can match legitimate carve-outs ("type /confirm to
    # authorise" inside a knowledge-tool answer), so don't gate on it.
    return cleaned, (n_banner + n_calls) > 0
