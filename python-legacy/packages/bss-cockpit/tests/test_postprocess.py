"""Unit tests for bss_cockpit.postprocess (v0.20.1).

Two helpers, both surface-agnostic:

* ``strip_channel_markup`` — removes Harmony / channel-format leakage
  (``<channel|>`` / ``<|channel|>`` / ``</channel>`` / leading
  ``assistantfinal``) so the REPL Panel and the browser bubble both
  start at the real content.
* ``knowledge_called`` — bool helper used by both surfaces to decide
  whether pipe-table rendering is legitimate this turn (renderer-less
  tool fired → prose IS the answer, tables are real; otherwise, pipes
  fall through as literal text per v0.19 doctrine).
"""

from __future__ import annotations

from bss_cockpit.postprocess import (
    knowledge_called,
    strip_channel_markup,
    strip_reasoning_leakage,
)

# ── strip_channel_markup ────────────────────────────────────────────


def test_strip_channel_markup_pipe_suffix_variant() -> None:
    """The most common shape — ``<channel|>`` at the very start of the
    bubble. gemma's final-message stream emits this when the model's
    response format isn't pinned by the gateway."""
    assert (
        strip_channel_markup("<channel|>To configure email...")
        == "To configure email..."
    )


def test_strip_channel_markup_bracketed_variant() -> None:
    """``<|channel|>`` — the fully-bracketed Harmony shape."""
    assert (
        strip_channel_markup("<|channel|>commentary text")
        == "commentary text"
    )


def test_strip_channel_markup_close_tag() -> None:
    """``</channel>`` close-side variant occasionally trails a bubble."""
    out = strip_channel_markup("answer text</channel>")
    assert "</channel>" not in out
    assert "answer text" in out


def test_strip_channel_markup_assistantfinal_marker() -> None:
    """Bare ``assistantfinal`` line at the start of the bubble — strip
    it but keep the body that follows."""
    out = strip_channel_markup("assistantfinal\nThe answer is 42.")
    assert "assistantfinal" not in out
    assert "The answer is 42." in out


def test_strip_channel_markup_case_insensitive() -> None:
    """Open-weight models occasionally upper-case the channel marker."""
    assert strip_channel_markup("<CHANNEL|>hi") == "hi"


def test_strip_channel_markup_idempotent() -> None:
    """Running the strip twice is a no-op once the markup is gone."""
    once = strip_channel_markup("<channel|>hello")
    twice = strip_channel_markup(once)
    assert once == twice == "hello"


def test_strip_channel_markup_no_artefacts_passes_through() -> None:
    """Plain text is untouched."""
    assert strip_channel_markup("Plain reply.") == "Plain reply."


def test_strip_channel_markup_empty_input() -> None:
    """Empty / None-ish inputs return unchanged so callers don't have
    to special-case them."""
    assert strip_channel_markup("") == ""


def test_strip_channel_markup_preserves_inner_pipes() -> None:
    """We only strip the markup TOKENS, not pipes that are part of the
    real content — pipe tables, Unix command lines, etc."""
    out = strip_channel_markup(
        "<channel|>Run `cat foo | grep bar` for the result."
    )
    assert "cat foo | grep bar" in out
    assert "<channel|>" not in out


# ── knowledge_called ────────────────────────────────────────────────


def test_knowledge_called_with_search_tool() -> None:
    """The most common case — ``knowledge.search`` fired this turn."""
    calls = [{"name": "knowledge.search", "args": {"q": "email config"}}]
    assert knowledge_called(calls) is True


def test_knowledge_called_with_get_tool() -> None:
    """``knowledge.get`` is the other surface; same carve-out."""
    calls = [{"name": "knowledge.get", "args": {"anchor": "x"}}]
    assert knowledge_called(calls) is True


def test_knowledge_called_with_other_tools_only() -> None:
    """Renderer-backed tools alone — no carve-out, tables stay literal."""
    calls = [
        {"name": "customer.get", "args": {}},
        {"name": "subscription.list_for_customer", "args": {}},
    ]
    assert knowledge_called(calls) is False


def test_knowledge_called_mixed_calls() -> None:
    """At least one ``knowledge.*`` flips the gate — even if other,
    renderer-backed tools also fired in the same turn."""
    calls = [
        {"name": "customer.get", "args": {}},
        {"name": "knowledge.search", "args": {"q": "..."}},
    ]
    assert knowledge_called(calls) is True


def test_knowledge_called_empty_list() -> None:
    """No tools fired (pure conversation) — pipes stay literal."""
    assert knowledge_called([]) is False


def test_knowledge_called_none_input() -> None:
    """``None`` is the legitimate ``captured_tool_calls`` shape when
    the cockpit hasn't tracked any calls yet — return False."""
    assert knowledge_called(None) is False


def test_knowledge_called_accepts_string_iterable() -> None:
    """Callers without args context can pass a plain list of names —
    the helper tolerates the simpler shape."""
    assert knowledge_called(["knowledge.search"]) is True
    assert knowledge_called(["customer.get", "order.list"]) is False


def test_knowledge_called_ignores_malformed_entries() -> None:
    """An entry without a string ``name`` is ignored, not crashed-on."""
    calls = [
        {"name": None, "args": {}},
        42,
        {"name": "knowledge.search"},
    ]
    assert knowledge_called(calls) is True


# ── strip_reasoning_leakage ─────────────────────────────────────────


def test_strip_think_xml_block() -> None:
    """``<think>...</think>`` block style — strip the whole block."""
    out = strip_reasoning_leakage(
        "<think>weighing options...</think>\nThe answer is 42."
    )
    assert "weighing" not in out
    assert "The answer is 42." in out


def test_strip_thinking_xml_block_long_form() -> None:
    """``<thinking>...</thinking>`` long-form variant."""
    out = strip_reasoning_leakage(
        "<thinking>step 1, step 2</thinking>\nDone."
    )
    assert "step" not in out
    assert "Done." in out


def test_strip_leading_thought_header_with_blank_line() -> None:
    """Bare ``thought\\n\\nAnswer.`` shape — the header sits on its
    own line, separated from the answer by a blank line."""
    out = strip_reasoning_leakage("thought\n\nThe answer is 42.")
    assert out == "The answer is 42."


def test_strip_inline_thought_prefix_v0_20_1() -> None:
    """v0.20.1 — gemma also leaks the prefix on the SAME line as the
    answer: ``thought Searched for ... but found nothing.`` The prior
    regex only caught the line-break shape; this same-line shape was
    flowing through to the REPL Panel and the operator saw it as part
    of the reply."""
    out = strip_reasoning_leakage(
        "thought Searched for 'msisdn pool' in the handbook but found no matches."
    )
    assert not out.lower().startswith("thought")
    assert out.startswith("Searched for")


def test_strip_inline_thinking_prefix_long_form() -> None:
    """Same as above but with the long-form ``thinking`` token."""
    out = strip_reasoning_leakage("thinking The answer is 42.")
    assert not out.lower().startswith("thinking")
    assert out.startswith("The answer is 42.")


def test_strip_does_not_eat_words_starting_with_thought() -> None:
    """The same-line strip MUST require a space after ``thought``,
    or it'd eat ``thoughtful``, ``thoughts``, etc. when those happen
    to start a reply."""
    out = strip_reasoning_leakage("Thoughtful answer follows.")
    assert out == "Thoughtful answer follows."
    out2 = strip_reasoning_leakage("Thoughts on the design:")
    assert out2 == "Thoughts on the design:"


def test_strip_does_not_touch_normal_text() -> None:
    """Plain prose is untouched."""
    out = strip_reasoning_leakage("Plain reply with no leakage.")
    assert out == "Plain reply with no leakage."


def test_strip_reasoning_leakage_empty_input() -> None:
    """Empty input returns unchanged so callers don't have to special
    case it."""
    assert strip_reasoning_leakage("") == ""


def test_strip_reasoning_leakage_idempotent() -> None:
    """Running twice is a no-op once the leakage is gone."""
    once = strip_reasoning_leakage("<think>x</think>\nHi")
    twice = strip_reasoning_leakage(once)
    assert once == twice == "Hi"
