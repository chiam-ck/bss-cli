"""v0.13 cross-surface session round trip (PR9).

The whole promise of v0.13 is that the CLI REPL and the browser
veneer are two surfaces over the same Conversation store. This file
asserts the round trip end-to-end:

1. "REPL" appends a user turn (we mimic the REPL by calling the
   shared Conversation API directly — same code path the REPL uses).
2. "Browser" GET /cockpit/{id} renders the page and shows that turn.
3. "Browser" POST /cockpit/{id}/turn appends another user turn.
4. "REPL" Conversation.resume({id}) sees the browser's turn.

The flake-rate is the doctrine watermark: per V0_13_0.md "the trap"
("REPL writes turn → exit → browser shows turn → browser writes turn
→ REPL --session resumes → REPL shows browser's turn. That round
trip is the entire promise of the release. If it works once in three
but flakes one in three, that's not done.").
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from bss_cockpit import Conversation, ConversationStore, configure_store
from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[3]


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
        pytest.skip("BSS_DB_URL not set; skipping cross-surface test.")
    return url


@pytest.fixture
def cross_client(db_url: str, monkeypatch):
    """Boot the CSR app + truncate cockpit.* before+after."""
    monkeypatch.setenv("BSS_DB_URL", db_url)

    async def _truncate():
        engine = create_async_engine(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            await s.execute(text(
                "TRUNCATE cockpit.pending_destructive, cockpit.message, "
                "cockpit.session RESTART IDENTITY CASCADE"
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_truncate())

    app = create_app(Settings())
    with TestClient(app) as c:
        yield c

    asyncio.run(_truncate())


@pytest.mark.parametrize("attempt", list(range(3)))
def test_repl_to_browser_to_repl_round_trip(
    cross_client: TestClient, db_url: str, attempt: int
) -> None:
    """The full round trip, run three times in a row.

    Doctrine: passes-once-fails-twice is not green. The
    parametrise(range(3)) gives us a single ``pytest -v`` summary
    that surfaces flake immediately.
    """
    # Surface A — "REPL" — opens a session and appends a user turn.
    # We use the Conversation API directly (same path the REPL uses)
    # via a fresh ConversationStore bound to the dev DB; the lifespan-
    # configured store on app.state is bound to the same DB so both
    # surfaces see the same rows.
    async def repl_open_and_append() -> str:
        engine = create_async_engine(db_url)
        try:
            store = ConversationStore(engine=engine)
            configure_store(store)
            conv = await store.open(actor="ck", label=f"round-trip-{attempt}")
            await conv.append_user_turn("opened from the REPL")
            return conv.session_id
        finally:
            await engine.dispose()

    sid = asyncio.run(repl_open_and_append())

    # Surface B — "Browser" — GETs the thread page and sees the REPL
    # turn rendered.
    page_a = cross_client.get(f"/cockpit/{sid}").text
    assert "opened from the REPL" in page_a

    # Surface B appends a turn via POST.
    r = cross_client.post(
        f"/cockpit/{sid}/turn",
        data={"message": "appended from the browser"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Surface A — "REPL" — resumes the same session and sees the
    # browser's turn in the transcript.
    async def repl_resume_and_read() -> str:
        engine = create_async_engine(db_url)
        try:
            store = ConversationStore(engine=engine)
            configure_store(store)
            conv = await store.resume(sid)
            return await conv.transcript_text()
        finally:
            await engine.dispose()

    transcript = asyncio.run(repl_resume_and_read())
    assert "opened from the REPL" in transcript
    assert "appended from the browser" in transcript
    # Order: REPL turn first, browser turn second.
    assert transcript.index("opened from the REPL") < transcript.index(
        "appended from the browser"
    )


def test_focus_set_on_browser_visible_to_repl(
    cross_client: TestClient, db_url: str
) -> None:
    """A focus pin set via the browser shows up on the REPL side."""
    async def repl_open() -> str:
        engine = create_async_engine(db_url)
        try:
            configure_store(ConversationStore(engine=engine))
            conv = await Conversation.open(actor="ck")
            return conv.session_id
        finally:
            await engine.dispose()

    sid = asyncio.run(repl_open())

    r = cross_client.post(
        f"/cockpit/{sid}/focus",
        data={"customer_id": "CUST-007"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def repl_check_focus() -> str | None:
        engine = create_async_engine(db_url)
        try:
            configure_store(ConversationStore(engine=engine))
            conv = await Conversation.resume(sid)
            return conv.customer_focus
        finally:
            await engine.dispose()

    assert asyncio.run(repl_check_focus()) == "CUST-007"


def test_reset_on_repl_visible_to_browser(
    cross_client: TestClient, db_url: str
) -> None:
    """Conversation.reset() on the REPL side wipes the messages the
    browser was rendering on its next page load."""
    async def setup() -> str:
        engine = create_async_engine(db_url)
        try:
            configure_store(ConversationStore(engine=engine))
            conv = await Conversation.open(actor="ck")
            await conv.append_user_turn("first turn")
            await conv.append_user_turn("second turn")
            return conv.session_id
        finally:
            await engine.dispose()

    sid = asyncio.run(setup())
    page_before = cross_client.get(f"/cockpit/{sid}").text
    assert "first turn" in page_before
    assert "second turn" in page_before

    async def reset() -> None:
        engine = create_async_engine(db_url)
        try:
            configure_store(ConversationStore(engine=engine))
            conv = await Conversation.resume(sid)
            await conv.reset()
        finally:
            await engine.dispose()

    asyncio.run(reset())
    page_after = cross_client.get(f"/cockpit/{sid}").text
    assert "first turn" not in page_after
    assert "second turn" not in page_after
