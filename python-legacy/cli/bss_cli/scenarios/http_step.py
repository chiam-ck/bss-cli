"""HTTP step runner for scenario YAML files.

v0.4 introduces the ``http:`` step type so the portal hero scenario
can drive the portal through its public HTTP surface (landing →
signup POST → SSE trigger → status poll) without reaching for bash
curls. The handler is intentionally small — FastAPI's ``TestClient``
semantics, not a full HTTP mock library — because the scenarios run
against the real portal container in compose.

Captures:
- Jsonpath ``capture:`` resolves against a synthetic result shape
  ``{status, headers, body, body_text}`` where ``body`` is the
  parsed JSON (or ``None``) and headers have lower-case keys.
- ``capture_regex:`` picks groups out of a named source (the common
  ``/signup?session=…`` → ``session_id`` case).

Polling: when ``poll`` is set the request is retried on the same
interval contract as the existing ``assert:`` step; the loop exits
on the first response whose ``expect:`` block matches, or the
deadline fires.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from jsonpath_ng.ext import parse as jsonpath_parse

from .context import ScenarioContext
from .schema import HTTPExpect, HTTPRegexCapture, HTTPStep

# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────


def _build_result(resp: httpx.Response, body_text: str | None = None) -> dict[str, Any]:
    text = body_text if body_text is not None else resp.text
    parsed: Any = None
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype and text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    return {
        "status": resp.status_code,
        "headers": {k.lower(): v for k, v in resp.headers.items()},
        "cookies": dict(resp.cookies),
        "body": parsed,
        "body_text": text or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Expect evaluation
# ─────────────────────────────────────────────────────────────────────────────


def _check_expect(
    expect: HTTPExpect, result: dict[str, Any], ctx: ScenarioContext
) -> list[str]:
    """Return a list of human-readable failure reasons (empty = pass)."""
    fails: list[str] = []
    if expect.status is not None:
        want = expect.status if isinstance(expect.status, list) else [expect.status]
        if result["status"] not in want:
            fails.append(f"status {result['status']} not in {want}")
    for raw in expect.body_contains:
        needle = ctx.interpolate(raw)
        if needle not in result["body_text"]:
            fails.append(f"body_contains: {needle!r} not found")
    for raw in expect.body_not_contains:
        needle = ctx.interpolate(raw)
        if needle in result["body_text"]:
            fails.append(f"body_not_contains: {needle!r} present")
    for key, want in expect.headers_match.items():
        got = result["headers"].get(key.lower())
        want_resolved = ctx.interpolate(want)
        if got != want_resolved:
            fails.append(f"header {key}={got!r}, expected {want_resolved!r}")
    for key, want in expect.body_json_equals.items():
        got = (result["body"] or {}).get(key) if isinstance(result["body"], dict) else None
        want_resolved = ctx.interpolate(want)
        if got != want_resolved:
            fails.append(f"body_json.{key}={got!r}, expected {want_resolved!r}")
    return fails


# ─────────────────────────────────────────────────────────────────────────────
# Request dispatch
# ─────────────────────────────────────────────────────────────────────────────


def _parse_method_url(http: str, base_url: str, ctx: ScenarioContext) -> tuple[str, str]:
    method, _, url = http.strip().partition(" ")
    method = method.upper()
    url = ctx.interpolate(url.strip())
    if not url.startswith(("http://", "https://")):
        resolved_base = ctx.interpolate(base_url)
        url = resolved_base.rstrip("/") + "/" + url.lstrip("/")
    return method, url


async def _do_request(step: HTTPStep, ctx: ScenarioContext) -> dict[str, Any]:
    method, url = _parse_method_url(step.http, step.base_url, ctx)
    headers = {k: str(ctx.interpolate(v)) for k, v in step.headers.items()}
    cookies = (
        {k: str(ctx.interpolate(v)) for k, v in step.cookies.items()}
        if step.cookies
        else None
    )
    form = ctx.interpolate(step.form) if step.form else None
    json_body = ctx.interpolate(step.json_body) if step.json_body is not None else None

    async with httpx.AsyncClient(
        timeout=step.timeout_seconds,
        follow_redirects=step.follow_redirects,
        cookies=cookies,
    ) as client:
        if step.drain_stream:
            # Consume the response to EOF so the server finishes streaming
            # (e.g. SSE endpoints only run to completion when the client
            # reads through). We keep the body in memory — acceptable for
            # scenarios but not for truly huge responses.
            body_chunks: list[bytes] = []
            async with client.stream(
                method,
                url,
                headers=headers,
                data=form,
                json=json_body,
            ) as response:
                async for chunk in response.aiter_bytes():
                    body_chunks.append(chunk)
                body_bytes = b"".join(body_chunks)
                return _build_result(response, body_text=body_bytes.decode("utf-8", errors="replace"))
        resp = await client.request(
            method,
            url,
            headers=headers,
            data=form,
            json=json_body,
        )
    return _build_result(resp)


# ─────────────────────────────────────────────────────────────────────────────
# Capture
# ─────────────────────────────────────────────────────────────────────────────


def _capture_jsonpath(result: dict[str, Any], captures: dict[str, str]) -> dict[str, Any]:
    newly: dict[str, Any] = {}
    for var_name, path_expr in captures.items():
        matches = jsonpath_parse(path_expr).find(result)
        if not matches:
            raise KeyError(
                f"capture {var_name!r}: jsonpath {path_expr!r} "
                f"matched nothing in HTTP result"
            )
        newly[var_name] = matches[0].value
    return newly


def _capture_regex(
    result: dict[str, Any], captures: dict[str, HTTPRegexCapture]
) -> dict[str, Any]:
    newly: dict[str, Any] = {}
    for var_name, cfg in captures.items():
        source = _resolve_source(result, cfg.source)
        if not isinstance(source, str):
            raise KeyError(
                f"capture_regex {var_name!r}: source {cfg.source!r} "
                f"did not resolve to a string (got {type(source).__name__})"
            )
        m = re.search(cfg.pattern, source)
        if not m:
            raise KeyError(
                f"capture_regex {var_name!r}: pattern {cfg.pattern!r} "
                f"did not match source"
            )
        newly[var_name] = m.group(cfg.group)
    return newly


def _resolve_source(result: dict[str, Any], path: str) -> Any:
    """Dot-path resolution for the synthetic HTTP result shape."""
    node: Any = result
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


# ─────────────────────────────────────────────────────────────────────────────
# Top-level run
# ─────────────────────────────────────────────────────────────────────────────


async def run_http_step(
    step: HTTPStep,
    ctx: ScenarioContext,
    *,
    format_error: Callable[[BaseException], str],
):
    """Execute an HTTP step; return a ``StepResult`` compatible with runner.py."""
    # Deferred import to avoid circular reference between http_step.py and runner.py.
    from .runner import StepResult

    t0 = time.monotonic()
    poll = step.poll
    deadline = time.monotonic() + (poll.timeout_seconds if poll else 0)
    interval = max((poll.interval_ms if poll else 0) / 1000.0, 0.05)

    last_fails: list[str] = []
    result: dict[str, Any] | None = None

    try:
        while True:
            result = await _do_request(step, ctx)
            last_fails = _check_expect(step.expect, result, ctx)
            if not last_fails:
                break
            if poll is None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(interval)
    except Exception as e:  # noqa: BLE001
        return StepResult(
            name=step.name,
            kind="http",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=format_error(e),
        )

    assert result is not None
    if last_fails:
        return StepResult(
            name=step.name,
            kind="http",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            result=result,
            error="; ".join(last_fails),
        )

    try:
        captured = _capture_jsonpath(result, step.capture)
        captured.update(_capture_regex(result, step.capture_regex))
    except KeyError as e:
        return StepResult(
            name=step.name,
            kind="http",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            result=result,
            error=str(e),
        )
    for name, value in captured.items():
        ctx.variables[name] = value

    return StepResult(
        name=step.name,
        kind="http",
        ok=True,
        duration_ms=(time.monotonic() - t0) * 1000,
        captured=captured,
        result=result,
    )
