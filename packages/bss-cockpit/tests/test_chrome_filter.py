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
