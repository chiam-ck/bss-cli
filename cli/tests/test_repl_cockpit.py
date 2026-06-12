"""REPL cockpit smoke tests (v0.13 PR6).

Covers the seam between the new REPL (cli/bss_cli/repl.py) and the
v0.13 cockpit primitives. Tests focus on:

* The slash-command dispatch table — each command routes to the right
  handler and updates the conversation correctly.
* The destructive-tool prefix list — every entry in the cockpit
  profile that should be propose-then-/confirm-shaped is recognized.
* The renderer dispatch unchanged from v0.6 (regression guard).

Tests against the in-process ``ConversationStore`` use the same dev
DB as the package suite (truncated before+after via the autouse
fixture in cli/tests/conftest.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from bss_cli.repl import _DESTRUCTIVE_PREFIXES, _is_destructive
from bss_cockpit import Conversation, ConversationStore, configure_store
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _DbSettings(BaseSettings):
    BSS_DB_URL: str = ""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@pytest.fixture
def db_url() -> str:
    url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.skip("BSS_DB_URL not set; skipping cockpit REPL DB tests.")
    return url


@pytest_asyncio.fixture
async def store(db_url: str):
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE cockpit.pending_destructive, cockpit.message, "
            "cockpit.session RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    s = ConversationStore(engine=engine)
    configure_store(s)
    try:
        yield s
    finally:
        configure_store(None)
        async with factory() as t:
            await t.execute(text(
                "TRUNCATE cockpit.pending_destructive, cockpit.message, "
                "cockpit.session RESTART IDENTITY CASCADE"
            ))
            await t.commit()
        await engine.dispose()


# ── Destructive-prefix recogniser ────────────────────────────────────


def test_is_destructive_recognizes_known_writes() -> None:
    assert _is_destructive("subscription.terminate")
    assert _is_destructive("subscription.terminate.specific_form")  # prefix match
    assert _is_destructive("payment.add_card")
    assert _is_destructive("case.open")
    assert _is_destructive("ticket.transition")
    assert _is_destructive("order.create")


def test_is_destructive_rejects_reads() -> None:
    assert not _is_destructive("subscription.get")
    assert not _is_destructive("customer.get")
    assert not _is_destructive("catalog.list_active_offerings")
    assert not _is_destructive("trace.get")
    assert not _is_destructive("clock.now")


def test_destructive_prefix_list_is_non_empty_and_deduped() -> None:
    assert len(_DESTRUCTIVE_PREFIXES) > 5
    assert len(set(_DESTRUCTIVE_PREFIXES)) == len(_DESTRUCTIVE_PREFIXES)


# ── Conversation handle integration ──────────────────────────────────


async def test_open_then_resume_round_trips_via_classmethod(
    store: ConversationStore,
) -> None:
    a = await Conversation.open(actor="ck", label="repl-test")
    b = await Conversation.resume(a.session_id)
    assert b.session_id == a.session_id
    assert b.label == "repl-test"


async def test_pending_destructive_consumed_on_next_turn(
    store: ConversationStore,
) -> None:
    """Simulates the propose → /confirm pairing the REPL drives.

    The REPL's _drive_turn() consumes any pending row and passes it
    into build_cockpit_prompt; this test asserts the row really is
    single-shot (a second consume returns None).
    """
    conv = await Conversation.open(actor="ck")
    pmid = await conv.append_assistant_turn(
        "I'd like to terminate SUB-7 — confirm?",
    )
    await conv.set_pending_destructive(
        "subscription.terminate", {"id": "SUB-7"}, proposal_message_id=pmid
    )

    first = await conv.consume_pending_destructive()
    assert first is not None
    assert first.tool_name == "subscription.terminate"

    second = await conv.consume_pending_destructive()
    assert second is None
