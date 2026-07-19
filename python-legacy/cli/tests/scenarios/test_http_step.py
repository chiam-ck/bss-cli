"""Scenario runner HTTP step — schema parse + dispatch + capture."""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml
from bss_cli.scenarios.context import ScenarioContext
from bss_cli.scenarios.http_step import run_http_step
from bss_cli.scenarios.schema import HTTPStep, Scenario


def _from_yaml(src: str) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(src))


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────


def test_http_step_parses_from_yaml() -> None:
    s = _from_yaml(
        """
name: http-scenario
steps:
  - name: landing
    http: GET /
    base_url: http://portal:8000
    expect:
      status: 200
      body_contains: [PLAN_M]
"""
    )
    step = s.steps[0]
    assert isinstance(step, HTTPStep)
    assert step.http == "GET /"
    assert step.expect.status == 200
    assert step.expect.body_contains == ["PLAN_M"]


def test_http_step_rejects_form_and_json_together() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError wraps the ValueError
        _from_yaml(
            """
name: bad
steps:
  - name: x
    http: POST /
    form: {a: 1}
    json: {b: 2}
"""
        )


# ─────────────────────────────────────────────────────────────────────────────
# Runtime
# ─────────────────────────────────────────────────────────────────────────────


def _format_error(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


@pytest.mark.asyncio
async def test_http_step_get_with_body_contains_match() -> None:
    ctx = ScenarioContext.new()
    step = HTTPStep(
        name="landing",
        http="GET /",
        base_url="http://portal:8000",
        expect={"status": 200, "body_contains": ["PLAN_M"]},
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://portal:8000/").mock(
            return_value=httpx.Response(200, text="<p>PLAN_M</p>")
        )
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert result.ok, result.error
    assert result.result["status"] == 200


@pytest.mark.asyncio
async def test_http_step_post_form_captures_regex_from_location() -> None:
    ctx = ScenarioContext.new()
    step = HTTPStep(
        name="signup submit",
        http="POST /signup",
        base_url="http://portal:8000",
        form={"plan": "PLAN_M", "name": "Demo"},
        expect={"status": 303},
        capture_regex={
            "session_id": {
                "source": "headers.location",
                "pattern": "session=([a-f0-9]+)",
            },
        },
    )
    with respx.mock() as mock:
        mock.post("http://portal:8000/signup").mock(
            return_value=httpx.Response(
                303,
                headers={"location": "/signup/PLAN_M/progress?session=deadbeef"},
            )
        )
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert result.ok, result.error
    assert result.captured == {"session_id": "deadbeef"}
    assert ctx.variables["session_id"] == "deadbeef"


@pytest.mark.asyncio
async def test_http_step_polls_until_body_contains() -> None:
    ctx = ScenarioContext.new()
    ctx.variables["sid"] = "abc123"
    step = HTTPStep(
        name="wait",
        http="GET /api/session/{{ sid }}",
        base_url="http://portal:8000",
        expect={"body_contains": ["\"done\": true"]},
        poll={"interval_ms": 50, "timeout_seconds": 2.0},
    )
    attempts = [
        httpx.Response(200, text='{"done": false}'),
        httpx.Response(200, text='{"done": false}'),
        httpx.Response(200, text='{"done": true, "subscription_id": "SUB-007"}'),
    ]

    with respx.mock() as mock:
        mock.get("http://portal:8000/api/session/abc123").mock(side_effect=attempts)
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert result.ok, result.error


@pytest.mark.asyncio
async def test_http_step_captures_body_json_via_jsonpath() -> None:
    ctx = ScenarioContext.new()
    step = HTTPStep(
        name="session",
        http="GET /api/session/x",
        base_url="http://portal:8000",
        expect={"status": 200},
        capture={"subscription_id": "$.body.subscription_id"},
    )
    with respx.mock() as mock:
        mock.get("http://portal:8000/api/session/x").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/json"},
                text='{"subscription_id": "SUB-007", "done": true}',
            )
        )
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert result.ok, result.error
    assert ctx.variables["subscription_id"] == "SUB-007"


@pytest.mark.asyncio
async def test_http_step_fails_on_status_mismatch() -> None:
    ctx = ScenarioContext.new()
    step = HTTPStep(
        name="landing",
        http="GET /",
        base_url="http://portal:8000",
        expect={"status": 200},
    )
    with respx.mock() as mock:
        mock.get("http://portal:8000/").mock(return_value=httpx.Response(500))
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert not result.ok
    assert "status 500" in (result.error or "")


@pytest.mark.asyncio
async def test_http_step_sends_cookies_and_captures_set_cookie() -> None:
    ctx = ScenarioContext.new()
    ctx.variables["session_token"] = "abc-123"

    step_send = HTTPStep(
        name="send cookie",
        http="GET /protected",
        base_url="http://portal-csr:8000",
        cookies={"bss_csr_session": "{{ session_token }}"},
        expect={"status": 200},
    )

    received: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        received["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, text="ok")

    with respx.mock() as mock:
        mock.get("http://portal-csr:8000/protected").mock(side_effect=_capture)
        result = await run_http_step(step_send, ctx, format_error=_format_error)
    assert result.ok, result.error
    assert "bss_csr_session=abc-123" in received["cookie"]


@pytest.mark.asyncio
async def test_http_step_captures_response_cookie_via_jsonpath() -> None:
    ctx = ScenarioContext.new()
    step = HTTPStep(
        name="login",
        http="POST /login",
        base_url="http://portal-csr:8000",
        form={"username": "ck"},
        expect={"status": 303},
        capture={"cookie_token": "$.cookies.bss_csr_session"},
    )
    with respx.mock() as mock:
        mock.post("http://portal-csr:8000/login").mock(
            return_value=httpx.Response(
                303,
                headers={
                    "location": "/search",
                    "set-cookie": "bss_csr_session=cookie-xyz; Path=/",
                },
            )
        )
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert result.ok, result.error
    assert ctx.variables["cookie_token"] == "cookie-xyz"


@pytest.mark.asyncio
async def test_http_step_drain_stream_reads_entire_response() -> None:
    ctx = ScenarioContext.new()
    step = HTTPStep(
        name="drive SSE",
        http="GET /stream",
        base_url="http://portal:8000",
        expect={"status": 200, "body_contains": ["event: redirect"]},
        drain_stream=True,
    )
    payload = b"event: message\ndata: hi\n\nevent: redirect\ndata: done\n\n"
    with respx.mock() as mock:
        mock.get("http://portal:8000/stream").mock(
            return_value=httpx.Response(200, content=payload)
        )
        result = await run_http_step(step, ctx, format_error=_format_error)
    assert result.ok, result.error
    assert "event: redirect" in result.result["body_text"]
