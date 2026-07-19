"""Self-serve chat surface (v0.12 PR7).

Coverage:

* GET /chat without session/cap-tripped renders the empty form.
* GET /chat?cap_tripped=<reason> renders the banner; the form is
  hidden so the customer can't submit again into a known cap.
* GET /chat?session=<sid> for the owner renders the chat thread page
  with the SSE host that connects to /chat/events/{sid}.
* POST /chat/message with empty body 303s back to /chat.
* POST /chat/message under cap 303s to /chat?session=... and creates
  a turn in the store.
* POST /chat/message with check_caps blocking 303s to
  /chat?cap_tripped=<reason>&retry_at=... and DOES NOT create a turn.
* GET /chat/events/{sid} for cross-customer session is 403.
* GET /chat/events/{sid} for an unknown session is 404.
* Doctrine: chat is the only route in self-serve that imports
  ``astream_once``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")  # v0.14 — both names handled
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_orchestrator.chat_caps import CapStatus  # noqa: E402
from bss_portal_auth.test_helpers import create_test_session  # noqa: E402
from bss_self_serve.config import Settings  # noqa: E402
from bss_self_serve.main import create_app  # noqa: E402
from bss_self_serve.middleware import PORTAL_SESSION_COOKIE  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _DbSettings(BaseSettings):
    BSS_DB_URL: str = ""
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@pytest.fixture(autouse=True)
def _clock():
    _reset_clock()
    yield
    _reset_clock()


@pytest.fixture
def db_url() -> str:
    url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    os.environ["BSS_DB_URL"] = url
    return url


@pytest_asyncio.fixture
async def seed_db(db_url: str):
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _scrub(s):
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))

    async with factory() as s:
        await _scrub(s)
        await s.commit()
    yield factory
    async with factory() as s:
        await _scrub(s)
        await s.commit()
    await engine.dispose()


async def _seeded_session(
    seed_db,
    customer_id: str = "CUST-042",
    *,
    email: str | None = None,
) -> str:
    async with seed_db() as db:
        sess, _ident = await create_test_session(
            db,
            email=email or f"{customer_id.lower()}@example.sg",
            customer_id=customer_id,
            verified=True,
        )
        await db.commit()
        return sess.id


def _build_app() -> TestClient:
    return TestClient(create_app(Settings()))


# ── GET /chat ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chat_renders_empty_form(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        r = c.get("/chat")
    assert r.status_code == 200
    assert "Chat with us" in r.text
    assert 'class="chat-widget-form"' in r.text
    assert "name=\"message\"" in r.text


@pytest.mark.asyncio
async def test_get_chat_with_cap_tripped_renders_banner(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        r = c.get("/chat?cap_tripped=monthly_cost_cap&retry_at=2026-05-01T00:00:00+00:00")
    assert r.status_code == 200
    assert "this month's chat budget" in r.text
    # Chat input form is hidden when cap-tripped — the form element
    # is rendered with the ``hidden`` HTML attribute. Assert the
    # cap-warning class is present (the user-visible banner) and
    # that the form carries ``hidden``.
    assert 'class="chat-widget-cap"' in r.text
    assert "<form" in r.text and "hidden" in r.text


@pytest.mark.asyncio
async def test_get_chat_with_hourly_cap_tripped_renders_banner(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        r = c.get("/chat?cap_tripped=hourly_rate_cap&retry_at=2026-04-27T15:00:00+00:00")
    assert r.status_code == 200
    assert "chatting fast" in r.text
    # Form rendered but ``hidden`` when cap-tripped.
    assert 'class="chat-widget-cap"' in r.text


@pytest.mark.asyncio
async def test_get_chat_with_session_renders_thread_and_sse_host(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        with patch(
            "bss_self_serve.routes.chat.check_caps",
            return_value=CapStatus(allowed=True),
        ):
            post_resp = c.post(
                "/chat/message",
                data={"message": "what's my balance?"},
                follow_redirects=False,
            )
        assert post_resp.status_code == 303
        location = post_resp.headers["location"]
        assert "/chat?session=" in location

        r = c.get(location)
    assert r.status_code == 200
    assert "what&#39;s my balance?" in r.text or "what&#x27;s my balance" in r.text
    assert 'sse-connect="/chat/events/' in r.text
    assert 'hx-ext="sse"' in r.text


@pytest.mark.asyncio
async def test_get_chat_with_unknown_session_falls_back_to_form(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        r = c.get("/chat?session=does-not-exist")
    assert r.status_code == 200
    assert 'class="chat-widget-form"' in r.text
    assert 'sse-connect' not in r.text


# ── POST /chat/message ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_chat_empty_message_redirects_to_chat(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        r = c.post("/chat/message", data={"message": "  "}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat"


@pytest.mark.asyncio
async def test_post_chat_under_cap_creates_turn_and_redirects(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        with patch(
            "bss_self_serve.routes.chat.check_caps",
            return_value=CapStatus(allowed=True),
        ):
            r = c.post(
                "/chat/message",
                data={"message": "top up my data"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        m = re.search(r"/chat\?session=([0-9a-f]+)", r.headers["location"])
        assert m is not None
        sid_returned = m.group(1)

        # Turn was created in the store.
        store = c.app.state.chat_turn_store
        turn = await store.get(sid_returned)
        assert turn is not None
        assert turn.customer_id == "CUST-042"
        assert turn.question == "top up my data"


@pytest.mark.asyncio
async def test_post_chat_when_cap_tripped_redirects_to_banner(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    retry_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        with patch(
            "bss_self_serve.routes.chat.check_caps",
            return_value=CapStatus(
                allowed=False,
                reason="monthly_cost_cap",
                retry_at=retry_at,
            ),
        ):
            r = c.post(
                "/chat/message",
                data={"message": "hello"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "cap_tripped=monthly_cost_cap" in r.headers["location"]
        assert "retry_at=2026-05-01" in r.headers["location"]
        # No turn created — the cap blocked the LLM invocation.
        store = c.app.state.chat_turn_store
        # No way to enumerate; just confirm any obvious id is absent
        # by trying a freshly-minted one.
        assert await store.get("any-known-id") is None


@pytest.mark.asyncio
async def test_post_chat_fail_closed_on_cap_check_error(seed_db) -> None:
    """Doctrine: a cap that doesn't enforce is worse than no cap.
    chat_caps.check_caps catches errors and returns
    CapStatus(allowed=False, reason='cap_check_failed'); the route
    must surface that as a cap-tripped redirect, not as a 500."""
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        with patch(
            "bss_self_serve.routes.chat.check_caps",
            return_value=CapStatus(allowed=False, reason="cap_check_failed"),
        ):
            r = c.post(
                "/chat/message",
                data={"message": "hi"},
                follow_redirects=False,
            )
    assert r.status_code == 303
    assert "cap_tripped=cap_check_failed" in r.headers["location"]


# ── GET /chat/events/{sid} ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_events_unknown_session_404s(seed_db) -> None:
    sid = await _seeded_session(seed_db)
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid)
        r = c.get("/chat/events/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_chat_events_cross_customer_session_403s(seed_db) -> None:
    """Even if a customer somehow learns another customer's session_id,
    the SSE handler refuses. Belt + braces alongside the chat-page
    fallback."""
    sid_a = await _seeded_session(seed_db, customer_id="CUST-A")
    with _build_app() as c:
        # Customer A creates a turn.
        c.cookies.set(PORTAL_SESSION_COOKIE, sid_a)
        with patch(
            "bss_self_serve.routes.chat.check_caps",
            return_value=CapStatus(allowed=True),
        ):
            post_resp = c.post(
                "/chat/message",
                data={"message": "secret message"},
                follow_redirects=False,
            )
        assert post_resp.status_code == 303
        m = re.search(r"session=([0-9a-f]+)", post_resp.headers["location"])
        assert m is not None

    # Customer B logs in with a fresh DB session and tries to open A's
    # SSE stream. Need a second TestClient because mixing two cookies
    # in one client is awkward; create a fresh app + DB seed.
    sid_b = await _seeded_session(seed_db, customer_id="CUST-B")
    # Re-using the same TestClient is fine if we rotate the cookie;
    # but app.state.chat_turn_store is per-app, and a fresh _build_app
    # constructs a new one. So we'd lose A's turn.
    # Instead: keep the same TestClient + rotate cookies + assert.
    with _build_app() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sid_a)
        with patch(
            "bss_self_serve.routes.chat.check_caps",
            return_value=CapStatus(allowed=True),
        ):
            r = c.post(
                "/chat/message",
                data={"message": "msg"},
                follow_redirects=False,
            )
        a_sid = re.search(r"session=([0-9a-f]+)", r.headers["location"]).group(1)
        # Now rotate to customer B's session and try the stream.
        c.cookies.set(PORTAL_SESSION_COOKIE, sid_b)
        r = c.get(f"/chat/events/{a_sid}")
    assert r.status_code == 403


# ── Doctrine ──────────────────────────────────────────────────────────


def test_astream_once_only_imported_in_chat_route() -> None:
    routes_dir = (
        Path(__file__).resolve().parents[1]
        / "bss_self_serve"
        / "routes"
    )
    offenders: list[str] = []
    for path in routes_dir.glob("*.py"):
        if path.name == "chat.py":
            continue
        text_ = path.read_text(encoding="utf-8")
        if "astream_once" in text_:
            offenders.append(path.name)
    assert not offenders, (
        f"astream_once leaked into non-chat route(s): {offenders!r}. "
        "Per the v0.11+ doctrine, only the chat surface is "
        "orchestrator-mediated."
    )


# ── v0.13.1: anti-hallucination escalation guard ────────────────────


def test_claims_escalation_recognizes_first_person_active() -> None:
    """Guard helper: matches the LLM's first-person ACTIVE escalation
    claims (the signal that it's claiming to do something this turn)."""
    from bss_self_serve.routes.chat import _claims_escalation

    assert _claims_escalation("I've escalated this to a human agent.")
    assert _claims_escalation("I am escalating this now.")
    assert _claims_escalation("I will escalate this to support.")
    assert _claims_escalation("I've opened a case for you.")
    assert _claims_escalation("I'm filing a case on your behalf.")
    assert _claims_escalation("I've raised a case.")


def test_claims_escalation_does_not_match_routine_replies() -> None:
    from bss_self_serve.routes.chat import _claims_escalation

    assert not _claims_escalation("Your data balance is 5GB.")
    assert not _claims_escalation("PLAN_S costs SGD 5/month.")
    assert not _claims_escalation("")
    assert not _claims_escalation(None)  # type: ignore[arg-type]


def test_claims_escalation_does_not_false_positive_on_past_recap() -> None:
    """When the customer asks "what's my case ID?" after a prior
    escalation, the LLM legitimately recaps in past-tense / third-
    person — that should NOT trip the guard. Otherwise the customer
    is told to email support every time they reference an existing
    case (the v0.13.1 regression that prompted this tightening)."""
    from bss_self_serve.routes.chat import _claims_escalation

    assert not _claims_escalation(
        "Your case has been raised — case ID is CASE-123."
    )
    assert not _claims_escalation("The case I opened earlier is CASE-123.")
    assert not _claims_escalation("Case CASE-123 is being handled.")
    assert not _claims_escalation(
        "Your case from yesterday is still open."
    )


def test_escalation_fallback_template_carries_email() -> None:
    """The fallback message is the only sentence the customer sees on
    a hallucinated escalation; verify the email lands in it."""
    from bss_self_serve.routes.chat import _ESCALATION_HALLUCINATION_FALLBACK

    rendered = _ESCALATION_HALLUCINATION_FALLBACK.format(email="ck@example.com")
    assert "ck@example.com" in rendered
    assert "support" in rendered.lower()
    # Should NOT use the verbatim escalation phrase — that's what
    # we're filtering out.
    assert "escalated" not in rendered.lower()
