"""Unit tests for Conversation + ConversationStore (v0.13 PR2).

Covers the API surface promised by phases/V0_13_0.md §1.3:
open / resume / list_for, append (user|assistant|tool),
transcript_text, reset, close, set_focus,
set/consume pending_destructive.
"""

from __future__ import annotations

import pytest
from bss_cockpit import (
    Conversation,
    ConversationStore,
    PendingDestructive,
)

# ── open / resume / list_for ─────────────────────────────────────────


async def test_open_creates_active_session(store: ConversationStore) -> None:
    conv = await store.open(actor="ck", label="diagnose CUST-001")

    assert conv.session_id.startswith("SES-")
    assert conv.actor == "ck"
    assert conv.state == "active"
    assert conv.label == "diagnose CUST-001"
    assert conv.customer_focus is None
    assert conv.allow_destructive is False


async def test_open_rejects_empty_actor(store: ConversationStore) -> None:
    with pytest.raises(ValueError):
        await store.open(actor="")


async def test_resume_round_trips(store: ConversationStore) -> None:
    a = await store.open(actor="ck", label="L1", customer_focus="CUST-001")
    b = await store.resume(a.session_id)

    assert b.session_id == a.session_id
    assert b.actor == "ck"
    assert b.label == "L1"
    assert b.customer_focus == "CUST-001"
    assert b.state == "active"


async def test_resume_unknown_id_raises(store: ConversationStore) -> None:
    with pytest.raises(LookupError):
        await store.resume("SES-19000101-deadbeef")


async def test_list_for_returns_active_only_by_default(
    store: ConversationStore,
) -> None:
    a = await store.open(actor="ck")
    b = await store.open(actor="ck", label="closed-one")
    await b.close()
    c = await store.open(actor="someone-else")  # different actor, excluded

    rows = await store.list_for("ck")
    assert {r.session_id for r in rows} == {a.session_id}
    # active_only=False includes closed
    rows_all = await store.list_for("ck", active_only=False)
    assert {r.session_id for r in rows_all} == {a.session_id, b.session_id}
    # Other actor sees their own
    other = await store.list_for("someone-else")
    assert {r.session_id for r in other} == {c.session_id}


async def test_list_for_carries_message_count(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    await conv.append_user_turn("hello")
    await conv.append_assistant_turn("hi")
    await conv.append_user_turn("again")

    rows = await store.list_for("ck")
    assert len(rows) == 1
    assert rows[0].message_count == 3


# ── append (user|assistant|tool) ─────────────────────────────────────


async def test_append_each_role(store: ConversationStore) -> None:
    conv = await store.open(actor="ck")
    uid = await conv.append_user_turn("show CUST-001")
    aid = await conv.append_assistant_turn(
        "Looking up CUST-001",
        tool_calls_json=[{"name": "customer.get", "args": {"id": "CUST-001"}}],
    )
    tid = await conv.append_tool_turn("customer.get", '{"id": "CUST-001"}')

    assert uid > 0
    assert aid > uid
    assert tid > aid


async def test_append_rejects_unknown_role(store: ConversationStore) -> None:
    conv = await store.open(actor="ck")
    with pytest.raises(ValueError):
        await conv._append_message(
            role="system", content="x", tool_calls_json=None
        )


# ── transcript_text ──────────────────────────────────────────────────


async def test_transcript_text_orders_by_created_at(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    await conv.append_user_turn("hello")
    await conv.append_assistant_turn("hi there")
    await conv.append_user_turn("how are you")

    transcript = await conv.transcript_text()
    assert "user:\nhello" in transcript
    assert "assistant:\nhi there" in transcript
    assert transcript.index("hello") < transcript.index("hi there")
    assert transcript.index("hi there") < transcript.index("how are you")
    # Tool turns add a tool[name] prefix
    await conv.append_tool_turn("customer.get", '{"id": "C-1"}')
    t2 = await conv.transcript_text()
    assert "tool[customer.get]:" in t2


async def test_transcript_empty_for_new_conversation(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    assert await conv.transcript_text() == ""


# ── reset / close ────────────────────────────────────────────────────


async def test_reset_clears_messages_keeps_session(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck", label="L")
    await conv.append_user_turn("x")
    await conv.set_pending_destructive(
        "subscription.terminate",
        {"id": "SUB-1"},
        proposal_message_id=1,
    )

    await conv.reset()

    # Messages gone; pending_destructive gone; session row remains
    again = await store.resume(conv.session_id)
    assert again.label == "L"
    assert (await again.transcript_text()) == ""
    assert (await again.consume_pending_destructive()) is None


async def test_close_marks_state_closed_idempotent(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    await conv.close()
    await conv.close()  # no error
    again = await store.resume(conv.session_id)
    assert again.state == "closed"


# ── set_focus ────────────────────────────────────────────────────────


async def test_set_focus_round_trips(store: ConversationStore) -> None:
    conv = await store.open(actor="ck")
    assert conv.customer_focus is None

    await conv.set_focus("CUST-007")
    again = await store.resume(conv.session_id)
    assert again.customer_focus == "CUST-007"

    await conv.set_focus(None)
    again = await store.resume(conv.session_id)
    assert again.customer_focus is None


# ── pending_destructive ──────────────────────────────────────────────


async def test_pending_destructive_set_and_consume(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    pmid = await conv.append_assistant_turn(
        "I'd like to terminate SUB-7 — confirm?",
        tool_calls_json=[
            {"name": "subscription.terminate", "args": {"id": "SUB-7"}}
        ],
    )

    await conv.set_pending_destructive(
        "subscription.terminate",
        {"id": "SUB-7", "reason": "operator request"},
        proposal_message_id=pmid,
    )

    consumed = await conv.consume_pending_destructive()
    assert isinstance(consumed, PendingDestructive)
    assert consumed.tool_name == "subscription.terminate"
    assert consumed.tool_args == {"id": "SUB-7", "reason": "operator request"}
    assert consumed.proposal_message_id == pmid

    # Consuming again returns None (single-shot)
    assert (await conv.consume_pending_destructive()) is None


async def test_pending_destructive_replaces_prior(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    pmid1 = await conv.append_assistant_turn("first propose")
    pmid2 = await conv.append_assistant_turn("second propose")

    await conv.set_pending_destructive(
        "subscription.terminate", {"id": "SUB-A"}, proposal_message_id=pmid1
    )
    await conv.set_pending_destructive(
        "vas.purchase_for_me", {"vas_id": "V-1"}, proposal_message_id=pmid2
    )

    consumed = await conv.consume_pending_destructive()
    assert consumed is not None
    assert consumed.tool_name == "vas.purchase_for_me"
    assert consumed.proposal_message_id == pmid2


# ── classmethod delegators ───────────────────────────────────────────


async def test_classmethods_delegate_to_default_store(
    store: ConversationStore,
) -> None:
    """``store`` fixture calls ``configure_store(store)`` so the
    classmethods should hit the same instance."""
    a = await Conversation.open(actor="ck", label="cls-test")
    assert a.session_id.startswith("SES-")

    b = await Conversation.resume(a.session_id)
    assert b.session_id == a.session_id

    rows = await Conversation.list_for("ck")
    assert any(r.session_id == a.session_id for r in rows)
