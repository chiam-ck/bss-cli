"""v1.5 — chrome-filter classifier unit tests.

Pins the inventory of cockpit-emitted chrome strings so a new fallback
bubble added to the cockpit without a matching prefix here fails CI
loudly. Mirrors loyalty-cli's test_v11_repl_chrome_filter.py shape.
"""

from __future__ import annotations

import pytest

from bss_cockpit.chrome_filter import (
    is_cockpit_chrome,
    strip_fake_propose,
)


# ─── is_cockpit_chrome: every known cockpit prefix is recognised ────────


@pytest.mark.parametrize(
    "content",
    [
        # Route error fallback.
        "Sorry — something went wrong. Please try again.",
        # Empty-final-after-tool-calls recovery (variants the cockpit
        # actually emits: single tool, multiple tools, with/without
        # surrounding parens). The cockpit phrases this as
        # "(The model called `<name>` but did not synthesise a final
        # answer. Send the same question again or rephrase to retry.)"
        "(The model called `customer.get` but did not synthesise "
        "a final answer. Send the same question again or rephrase to retry.)",
        "(The model called `knowledge.search, catalog.list_offerings` "
        "but did not synthesise a final answer. Send the same question "
        "again or rephrase to retry.)",
        # Total empty-AIMessage fallback (no tool calls at all).
        "(no reply)",
        # Citation-guard fallback (_KNOWLEDGE_HALLUCINATION_FALLBACK
        # starts with "I don't have a citation for that.").
        "I don't have a citation for that. Run `bss admin knowledge "
        'search "<your query>"` or open `docs/HANDBOOK.md` for the '
        "authoritative answer.",
    ],
)
def test_known_chrome_prefixes_classified_as_chrome(content: str) -> None:
    assert is_cockpit_chrome(content) is True, content


def test_empty_and_whitespace_treated_as_chrome() -> None:
    # The cockpit never persists empty real replies — they get
    # replaced before persist. So anything blank IS chrome.
    assert is_cockpit_chrome("") is True
    assert is_cockpit_chrome("   ") is True
    assert is_cockpit_chrome("\n\n\t  \n") is True


# ─── is_cockpit_chrome: NO false positives on real LLM replies ─────────


@pytest.mark.parametrize(
    "content",
    [
        "Catalog above.",
        "Done.",
        "Found 3 customers matching the prefix.",
        "Pick a plan to drill into.",
        "The customer is on PLAN_M with 2.3 GB remaining. No open cases.",
        # Looks vaguely chrome-shaped but isn't an exact prefix match.
        "Sorry, I couldn't find a customer matching that email.",
        # Knowledge tool with prose answer (carve-out from the
        # one-sentence rule); prose may use the word "reply" or
        # "synthesise" without being chrome.
        "Per HANDBOOK §8.4, you rotate tokens by editing .env and "
        "restarting the service. No need to synthesise a new pepper.",
        "I'd reply with the cancellation flow but you asked about "
        "renewal — let me check the docs.",
        # Multi-line legitimate reply — must not match the (no reply)
        # prefix because the first line isn't that exact string.
        "The cancellation went through.\n(no follow-up needed)",
    ],
)
def test_legitimate_llm_replies_not_classified_as_chrome(content: str) -> None:
    assert is_cockpit_chrome(content) is False, content


# ─── strip_fake_propose: LLM-mimicked PROPOSE banners get stripped ─────


def test_strip_fake_propose_removes_propose_line() -> None:
    text = (
        "Let me set that up for you.\n"
        "⚠ PROPOSE: subscription.terminate subscription_id='SUB-001' "
        "(destructive — /confirm to execute)\n"
        "Once you /confirm I'll proceed."
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    assert "PROPOSE" not in cleaned
    assert "Let me set that up for you" in cleaned
    assert "Once you /confirm I'll proceed" in cleaned


def test_strip_fake_propose_case_insensitive() -> None:
    text = "propose: customer.close customer_id='CUST-001'"
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    assert cleaned == ""


def test_strip_fake_propose_matches_step_variants() -> None:
    text = "PROPOSE [step 2]: order.cancel order_id='ORD-007'"
    _, modified = strip_fake_propose(text)
    assert modified is True


def test_strip_fake_propose_leaves_real_prose_alone() -> None:
    text = "I propose we wait until you confirm the cancellation."
    cleaned, modified = strip_fake_propose(text)
    assert modified is False
    assert cleaned == text.strip()


def test_strip_fake_propose_handles_unicode_warning_symbol() -> None:
    # The cockpit's actual propose banner leads with U+26A0 + space.
    # If the LLM mimics that exactly, strip it.
    text = (
        "⚠ PROPOSE: subscription.terminate "
        "subscription_id='SUB-009'"
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    assert "PROPOSE" not in cleaned


# ─── v1.5: narrated-call mimicry (the Gemma "I propose ..." shape) ─────


def test_strip_narrated_call_gemma_shape_observed_in_wild() -> None:
    # Verbatim Gemma output from a live REPL session (2026-05-26):
    # "I propose to terminate subscription SUB-0005.
    #  subscription.terminate(subscription_id=\"SUB-0005\")
    #
    #  Please type /confirm to proceed."
    # The bubble looks like a real propose but no tool_call was emitted,
    # so /confirm stalls. strip_fake_propose must remove the call shape
    # AND the "type /confirm" prompt so what reaches the operator
    # doesn't lie about being actionable.
    text = (
        "I propose to terminate subscription SUB-0005. "
        'subscription.terminate(subscription_id="SUB-0005")\n\n'
        "Please type /confirm to proceed."
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    assert "subscription.terminate(" not in cleaned
    # The "type /confirm" sentence is also dropped — leaving it would
    # be a second lie about what's about to happen.
    assert "type /confirm" not in cleaned.lower()


def test_strip_narrated_call_multiple_tools_in_prose() -> None:
    text = (
        "I'll first call customer.get(customer_id='CUST-001') and then "
        "subscription.list_for_customer(customer_id='CUST-001'). Type "
        "/confirm to proceed."
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    assert "customer.get(" not in cleaned
    assert "subscription.list_for_customer(" not in cleaned


def test_strip_does_not_match_dotted_prose_without_call_parens() -> None:
    # "subscription.state" or "customer.get" without parens is prose
    # describing an idea, not a call — leave it alone.
    text = (
        "The subscription.state field flips to 'active' once "
        "customer.attest_kyc succeeds."
    )
    cleaned, modified = strip_fake_propose(text)
    # Banner stripper finds nothing AND call-shape regex requires (...)
    # so this prose survives intact.
    assert modified is False
    assert cleaned == text.strip()


def test_strip_preserves_legitimate_prose_around_narrated_call() -> None:
    text = (
        "Found the line. Here's the plan: "
        "subscription.terminate(subscription_id='SUB-007'). Want me to "
        "proceed?"
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    # The call shape is gone but the surrounding prose survives so the
    # operator can still see the ask.
    assert "Found the line" in cleaned
    assert "Want me to proceed" in cleaned
    assert "subscription.terminate(" not in cleaned


# ─── v1.5 follow-up: half-stripped propose observed in live REPL ───────


def test_strip_handles_backtick_wrapped_call_no_empty_backticks_left() -> None:
    # Observed verbatim in a live REPL session (2026-05-26 follow-up):
    # the LLM wrapped the narrated call in single backticks, the
    # original regex stripped only the call body, leaving "I propose
    # to terminate subscription SUB-0005. ``" as a half-finished
    # propose that still misled the operator into typing /confirm.
    # The broader regex now eats the backticks AND the "I propose ..."
    # narration adjacent to a stripped call.
    text = (
        "I propose to terminate subscription SUB-0005. "
        '`subscription.terminate(subscription_id="SUB-0005")`'
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    # No half-finished propose left over.
    assert "I propose to terminate" not in cleaned
    assert "subscription.terminate" not in cleaned
    assert "``" not in cleaned
    # Cleaned-to-empty is fine here — the caller's mimicry-stall
    # warning takes over and replaces with the canonical explanation.
    assert cleaned.strip() == ""


def test_strip_handles_triple_backtick_code_fence_call() -> None:
    # Some models fence the call in triple backticks. Same defense
    # for the call + backticks. The surrounding prose ("I'll cancel
    # that order now:") survives — the narration regex deliberately
    # requires "I [will|propose|...] to <destructive verb>" with the
    # "to" present, so vague prose stays intact (false positives are
    # worse than false negatives here — better to leave a slightly
    # awkward bubble than to delete legitimate operator-facing text).
    text = (
        "I would like to cancel that order now:\n"
        '```order.cancel(order_id="ORD-007")```'
    )
    cleaned, modified = strip_fake_propose(text)
    assert modified is True
    assert "order.cancel(" not in cleaned
    assert "```" not in cleaned
    # "I would like to cancel ..." MATCHES the narration regex
    # (destructive canon + "to" + "cancel") so it gets stripped.
    assert "I would like to cancel" not in cleaned


def test_narration_strip_only_fires_with_destructive_canon() -> None:
    # "I'm going to think about it for a moment" — no destructive verb,
    # no call shape, no strip. Survives intact.
    text = "I'm going to think about it for a moment."
    cleaned, modified = strip_fake_propose(text)
    assert modified is False
    assert cleaned == text


def test_narration_strip_does_not_run_without_a_stripped_call() -> None:
    # "I propose we look at the metrics first" has the narration shape
    # but no function-call shape — the narration strip only runs when
    # a call was actually removed (the destructive context is what
    # makes the narration misleading).
    text = "I propose we look at the metrics first."
    cleaned, modified = strip_fake_propose(text)
    assert modified is False
    assert cleaned == text


# ─── Inventory guard — new chrome MUST be added to the module ──────────


def test_chrome_prefix_inventory_locked() -> None:
    # The list lives in chrome_filter._ASSISTANT_CHROME_PREFIXES. We
    # poke at the private attr deliberately — when someone adds a new
    # fallback bubble to the cockpit they will land a new prefix
    # entry too, and this test pins the inventory so the omission
    # surfaces in code review rather than silently letting chrome
    # back into LLM history.
    from bss_cockpit.chrome_filter import _ASSISTANT_CHROME_PREFIXES

    assert _ASSISTANT_CHROME_PREFIXES == (
        "Sorry — something went wrong",
        "(The model called ",
        "(no reply)",
        "I don't have a citation for that",
    )
