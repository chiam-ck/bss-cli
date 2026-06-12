"""Conversation.list_messages — structured row view of the message log
(v0.13.1).

Replaces the lossy ``transcript_text()`` round-trip in template /
renderer paths. The previous shape serialised to text + re-parsed on
``\\n\\n`` boundaries, which truncated assistant bubbles whose body
contained blank lines (e.g. paragraph break + markdown table). This
file pins the structured-row contract.
"""

from __future__ import annotations

from bss_cockpit import ConversationMessage, ConversationStore


async def test_list_messages_returns_each_row(store: ConversationStore) -> None:
    conv = await store.open(actor="ck")
    await conv.append_user_turn("hello")
    await conv.append_assistant_turn("hi")
    await conv.append_tool_turn("customer.get", '{"id": "C-1"}')

    rows = await conv.list_messages()
    assert len(rows) == 3
    assert all(isinstance(r, ConversationMessage) for r in rows)
    assert [r.role for r in rows] == ["user", "assistant", "tool"]
    assert rows[2].tool_name == "customer.get"


async def test_list_messages_preserves_blank_lines_in_body(
    store: ConversationStore,
) -> None:
    """The bug this fixes: a markdown-table assistant reply with a
    paragraph break would get truncated by the transcript_text +
    \\n\\n-split round trip. list_messages reads rows directly and
    must preserve the body verbatim."""
    conv = await store.open(actor="ck")
    body = (
        "Here is the catalog:\n\n"
        "| Plan | Price |\n"
        "|------|-------|\n"
        "| PLAN_S | $5 |\n\n"
        "Let me know if you want to switch."
    )
    await conv.append_assistant_turn(body)
    rows = await conv.list_messages()
    assert len(rows) == 1
    assert rows[0].content == body
    assert "| PLAN_S | $5 |" in rows[0].content


async def test_list_messages_orders_by_created_at_then_id(
    store: ConversationStore,
) -> None:
    conv = await store.open(actor="ck")
    a = await conv.append_user_turn("first")
    b = await conv.append_assistant_turn("second")
    c = await conv.append_user_turn("third")
    rows = await conv.list_messages()
    assert [r.id for r in rows] == [a, b, c]
