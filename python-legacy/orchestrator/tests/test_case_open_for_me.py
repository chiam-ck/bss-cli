"""case.open_for_me + case.show_transcript_for + customer-chat prompt
(v0.12 PR6).

Coverage:

1. ``EscalationCategory`` enum has exactly six members (the five
   non-negotiable + ``other``).
2. Each category maps to a CRM CaseCategory + priority.
3. ``case.open_for_me`` hashes the transcript with SHA-256, persists
   it idempotently, and opens the case with the hash linked.
4. Hash is deterministic for identical transcripts.
5. ``case.show_transcript_for`` resolves the hash, handles missing
   hash, surfaces the body.
6. Customer-chat prompt template fills variables, mentions all five
   categories, and emits the three verbatim sentences.
"""

from __future__ import annotations

import hashlib
from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest
from bss_orchestrator import auth_context
from bss_orchestrator.customer_chat_prompt import (
    build_balance_summary,
    build_customer_chat_prompt,
)
from bss_orchestrator.tools import TOOL_PROFILES
from bss_orchestrator.tools.mine_wrappers import (
    _ESCALATION_TO_CASE_CATEGORY,
    _ESCALATION_TO_PRIORITY,
    case_open_for_me,
)
from bss_orchestrator.types import EscalationCategory

# ─── 1. Enum integrity ──────────────────────────────────────────────


def test_escalation_category_has_exactly_six_members() -> None:
    members = set(get_args(EscalationCategory))
    assert members == {
        "fraud",
        "billing_dispute",
        "regulator_complaint",
        "identity_recovery",
        "bereavement",
        "other",
    }


def test_each_escalation_category_has_priority_and_case_category() -> None:
    for cat in get_args(EscalationCategory):
        assert cat in _ESCALATION_TO_CASE_CATEGORY
        assert cat in _ESCALATION_TO_PRIORITY
        assert _ESCALATION_TO_PRIORITY[cat] in {"low", "medium", "high", "critical"}


# ─── 2. case.open_for_me behaviour ──────────────────────────────────


@pytest.fixture
def reset_actor_after():
    yield
    token = auth_context.set_actor("__test_clear__")
    auth_context.reset_actor(token)


@pytest.mark.asyncio
async def test_case_open_for_me_hashes_and_persists_transcript(
    reset_actor_after,
) -> None:
    transcript = (
        "User: I think someone took over my account\n"
        "Assistant: That sounds like fraud — I'll escalate.\n"
    )
    expected_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

    fake_clients = AsyncMock()
    fake_clients.crm.store_chat_transcript = AsyncMock(
        return_value={"hash": expected_hash}
    )
    fake_clients.crm.open_case = AsyncMock(
        return_value={
            "id": "CASE-FOR-ME",
            "customerId": "CUST-042",
            "chatTranscriptHash": expected_hash,
        }
    )

    token = auth_context.set_actor("CUST-042", transcript=transcript)
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await case_open_for_me(
                category="fraud",
                subject="Suspected account takeover",
                description="Customer reports unrecognised charges and password change.",
            )
    finally:
        auth_context.reset_actor(token)

    # Transcript was stored first with the right hash.
    fake_clients.crm.store_chat_transcript.assert_awaited_once_with(
        hash_=expected_hash,
        customer_id="CUST-042",
        body=transcript,
    )
    # Case was opened referencing that hash.
    open_args = fake_clients.crm.open_case.call_args.kwargs
    assert open_args["customer_id"] == "CUST-042"
    assert open_args["chat_transcript_hash"] == expected_hash
    assert open_args["category"] == _ESCALATION_TO_CASE_CATEGORY["fraud"]
    assert open_args["priority"] == _ESCALATION_TO_PRIORITY["fraud"]
    assert open_args["description"].startswith("[fraud] ")
    assert result["chatTranscriptHash"] == expected_hash


@pytest.mark.asyncio
async def test_case_open_for_me_hash_deterministic_for_same_body(
    reset_actor_after,
) -> None:
    transcript = "User: same conversation\nAssistant: same reply\n"
    expected_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

    fake_clients = AsyncMock()
    fake_clients.crm.store_chat_transcript = AsyncMock(return_value={})
    fake_clients.crm.open_case = AsyncMock(return_value={"id": "X"})

    token = auth_context.set_actor("CUST-042", transcript=transcript)
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            await case_open_for_me(
                category="other", subject="x", description="x"
            )
            await case_open_for_me(
                category="other", subject="x", description="x"
            )
    finally:
        auth_context.reset_actor(token)

    # Both calls used the same hash → store_chat_transcript can rely
    # on its server-side idempotency.
    h0 = fake_clients.crm.store_chat_transcript.call_args_list[0].kwargs["hash_"]
    h1 = fake_clients.crm.store_chat_transcript.call_args_list[1].kwargs["hash_"]
    assert h0 == h1 == expected_hash


@pytest.mark.asyncio
async def test_case_open_for_me_requires_actor_bound() -> None:
    from bss_orchestrator.tools.mine_wrappers import _NoActorBound

    with pytest.raises(_NoActorBound):
        await case_open_for_me(
            category="fraud", subject="x", description="x"
        )


@pytest.mark.asyncio
async def test_case_open_for_me_each_category_routes_correctly(
    reset_actor_after,
) -> None:
    fake_clients = AsyncMock()
    fake_clients.crm.store_chat_transcript = AsyncMock(return_value={})
    fake_clients.crm.open_case = AsyncMock(return_value={"id": "CASE-X"})

    token = auth_context.set_actor("CUST-042", transcript="t")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            for cat in get_args(EscalationCategory):
                await case_open_for_me(
                    category=cat, subject="s", description="d"
                )
    finally:
        auth_context.reset_actor(token)

    # Six calls, each with the right CRM category + priority mapping.
    calls = fake_clients.crm.open_case.call_args_list
    assert len(calls) == 6
    seen = {c.kwargs["description"].split("]")[0][1:] for c in calls}
    assert seen == {
        "fraud",
        "billing_dispute",
        "regulator_complaint",
        "identity_recovery",
        "bereavement",
        "other",
    }


# ─── 3. case.show_transcript_for behaviour ──────────────────────────


@pytest.mark.asyncio
async def test_show_transcript_for_returns_body_when_hash_present() -> None:
    fake_clients = AsyncMock()
    fake_clients.crm.get_case = AsyncMock(
        return_value={
            "id": "CASE-X",
            "customerId": "CUST-042",
            "chatTranscriptHash": "abc123",
        }
    )
    fake_clients.crm.get_chat_transcript = AsyncMock(
        return_value={
            "hash": "abc123",
            "customer_id": "CUST-042",
            "body": "User said hello\n",
            "recorded_at": "2026-04-27T14:00:00Z",
        }
    )

    from bss_orchestrator.tools.case import case_show_transcript_for

    with patch(
        "bss_orchestrator.tools.case.get_clients", return_value=fake_clients
    ):
        result = await case_show_transcript_for("CASE-X")

    assert "User said hello" in result["body"]
    fake_clients.crm.get_chat_transcript.assert_awaited_once_with("abc123")


@pytest.mark.asyncio
async def test_show_transcript_for_returns_none_when_hash_absent() -> None:
    fake_clients = AsyncMock()
    fake_clients.crm.get_case = AsyncMock(
        return_value={"id": "CASE-Y", "chatTranscriptHash": None}
    )
    from bss_orchestrator.tools.case import case_show_transcript_for

    with patch(
        "bss_orchestrator.tools.case.get_clients", return_value=fake_clients
    ):
        result = await case_show_transcript_for("CASE-Y")

    assert result == {"transcript": None, "reason": "no_transcript_linked"}
    fake_clients.crm.get_chat_transcript.assert_not_called()


def test_case_show_transcript_for_is_not_in_customer_profile() -> None:
    """CSR-side tool: must NOT be reachable from the chat surface,
    where leaking another customer's transcript would be the worst
    case the trip-wire is designed to prevent."""
    assert "case.show_transcript_for" not in TOOL_PROFILES["customer_self_serve"]


def test_case_open_for_me_in_customer_profile_with_ownership_path() -> None:
    from bss_orchestrator.ownership import OWNERSHIP_PATHS

    assert "case.open_for_me" in TOOL_PROFILES["customer_self_serve"]
    assert "case.open_for_me" in OWNERSHIP_PATHS
    assert OWNERSHIP_PATHS["case.open_for_me"] == ["customerId"]


# ─── 4. Customer-chat prompt template ───────────────────────────────


def test_prompt_renders_with_variables() -> None:
    prompt = build_customer_chat_prompt(
        customer_name="Ck",
        customer_email="ck@example.com",
        account_state="active",
        current_plan="PLAN_M",
        balance_summary="data 5/10 GB, voice 100/200 minutes",
    )
    assert "Ck" in prompt
    assert "ck@example.com" in prompt
    assert "PLAN_M" in prompt
    assert "data 5/10 GB" in prompt


def test_prompt_lists_all_five_escalation_categories() -> None:
    prompt = build_customer_chat_prompt(
        customer_name="X", customer_email="x@x"
    )
    for cat in (
        "fraud",
        "billing_dispute",
        "regulator_complaint",
        "identity_recovery",
        "bereavement",
    ):
        assert cat in prompt, f"Prompt is missing the {cat!r} category"


def test_prompt_carries_three_verbatim_sentences() -> None:
    # Normalise whitespace — the prompt wraps lines for readability,
    # but the LLM stitches them at inference time. We assert presence
    # of the canonical phrasing modulo whitespace.
    prompt = " ".join(
        build_customer_chat_prompt(
            customer_name="X", customer_email="x@x"
        ).split()
    )
    assert "I've topped up your line" in prompt
    assert "I've scheduled your switch to" in prompt
    assert "I've opened a case for this" in prompt
    assert "follow up via email at" in prompt
    # Doctrine: no specific-time promise. The platform does not send
    # automated case-update emails (case events publish to RabbitMQ
    # but no subscriber delivers an email today; follow-up is
    # operator-driven). The LLM must not commit the system to a
    # turnaround it cannot keep.
    assert "24 hours" not in prompt
    assert "within 24" not in prompt
    assert "by end of day" not in prompt


def test_prompt_does_not_leak_model_identity() -> None:
    """Trap clause: the customer must not see model details."""
    prompt = build_customer_chat_prompt(
        customer_name="X", customer_email="x@x"
    )
    for taboo in ("Gemma", "MiMo", "OpenRouter", "Anthropic", "GPT", "Claude"):
        assert taboo.lower() not in prompt.lower(), (
            f"Customer-chat prompt leaks model identity ({taboo!r})"
        )


def test_prompt_handles_missing_variables_gracefully() -> None:
    # Mid-signup: subscription hasn't loaded; balance_summary unset.
    prompt = build_customer_chat_prompt(
        customer_name="", customer_email=""
    )
    # Renders without raising; placeholders prevent fabrication.
    assert "(loading)" in prompt
    assert "your address on file" in prompt


# ─── v0.19+ — operator support email + self-serve URL guidance ──────


def test_prompt_uses_operator_support_email_not_customer_email() -> None:
    """Regression: previous template substituted ``{customer_email}``
    in the "email support directly at …" sentence, telling customers
    to email themselves. The fallback inbox is operator-side."""
    prompt = build_customer_chat_prompt(
        customer_name="Ck",
        customer_email="customer@example.com",
        operator_support_email="support@bss-cli.com",
    )
    # The customer's own email still appears in the verbatim escalation
    # sentence ("you'll hear back via email at …") — that's the right
    # use of the variable.
    assert "customer@example.com" in prompt
    # The OPERATOR address appears in the "human support fallback"
    # section, NOT the customer's address.
    assert "support@bss-cli.com" in prompt
    # And we never tell the customer to email themselves.
    assert "email support directly at customer@example.com" not in prompt


def test_prompt_lists_self_serve_pages_for_routine_operations() -> None:
    """The customer chat must point at the portal's self-serve pages
    for things the customer can already do without human help. Naming
    a self-serve URL is doctrine; suggesting "email support" for a
    contact-detail change is the bug we just fixed."""
    prompt = build_customer_chat_prompt(
        customer_name="X",
        customer_email="x@x",
        operator_support_email="support@bss-cli.com",
    )
    # Each routine operation has a path the LLM can quote.
    for path in (
        "/profile/contact/email/change",
        "/profile/contact/phone/update",
        "/profile/contact/address/update",
        "/profile/contact/name/update",
        "/payment-methods",
        "/payment-methods/add",
        "/billing/history",
        "/plan/change",
    ):
        assert path in prompt, (
            f"Self-serve URL {path!r} is missing from the customer chat prompt; "
            f"the LLM has to know where to send the customer"
        )


def test_anonymous_prompt_uses_operator_support_email_for_pre_signup_fallback() -> None:
    prompt = build_customer_chat_prompt(
        customer_name="",
        customer_email="visitor@example.com",
        operator_support_email="support@bss-cli.com",
        is_linked=False,
    )
    assert "support@bss-cli.com" in prompt
    # Pre-signup fallback should NOT direct the visitor to themselves.
    assert "contact support directly at visitor@example.com" not in prompt


def test_balance_summary_renderer_handles_unlimited_and_capped() -> None:
    sub = {
        "balances": [
            {"type": "data", "used": 5, "total": 10, "unit": "GB"},
            {"type": "voice", "used": 50, "total": None, "unit": "minutes"},
        ]
    }
    line = build_balance_summary(sub)
    assert "data 5/10 GB" in line
    assert "voice unlimited" in line


def test_balance_summary_renderer_handles_missing_subscription() -> None:
    assert build_balance_summary(None) == "(loading)"
    assert build_balance_summary({}) == "(loading)"
