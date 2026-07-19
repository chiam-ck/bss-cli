"""Pydantic schema for scenario YAML.

The schema is intentionally narrow — v0.1 supports: variables, setup,
steps (action/ask/assert), teardown. No conditionals, loops, or scenario
composition. Those are Phase-11+ concerns per PHASE_10.md's scope guard.

Any unknown field at any level is a validation error — fail loud rather
than silently ignore typos in scenario files.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ─────────────────────────────────────────────────────────────────────────────
# Setup / teardown
# ─────────────────────────────────────────────────────────────────────────────


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reset_operational_data: bool = False
    reset_sequences: bool = False
    freeze_clock_at: str | None = None  # ISO-8601 instant, optional tz
    variables: dict[str, Any] = Field(default_factory=dict)


class Teardown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unfreeze_clock: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Polling + operators (used by assertions and expect_final_state)
# ─────────────────────────────────────────────────────────────────────────────


class Poll(BaseModel):
    """Polling config for assertions against eventually-consistent state."""

    model_config = ConfigDict(extra="forbid")

    interval_ms: int = 200
    timeout_seconds: float = 5.0


# ─────────────────────────────────────────────────────────────────────────────
# Step types
# ─────────────────────────────────────────────────────────────────────────────


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # Optional: assign captured values from tool result (jsonpath-ng expressions)
    capture: dict[str, str] = Field(default_factory=dict)


class ActionStep(_StepBase):
    """A deterministic tool call. ``action`` is a dotted registry name."""

    action: str
    args: dict[str, Any] = Field(default_factory=dict)


class AskStep(_StepBase):
    """An LLM-driven step. ``ask`` is natural language with `{{ var }}` interp."""

    ask: str
    timeout_seconds: float = 60.0
    expect_tools_called_include: list[str] = Field(default_factory=list)
    expect_tools_not_called: list[str] = Field(default_factory=list)
    expect_final_state: dict[str, Any] = Field(default_factory=dict)
    expect_event_sequence: list[str] = Field(default_factory=list)
    allow_clarification: bool = False


class AssertCall(BaseModel):
    """The `assert:` step block — call a read tool and check the shape."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Dot-path keys → expected scalar or operator dict
    expect: dict[str, Any] = Field(default_factory=dict)
    poll: Poll | None = None


class AssertStep(_StepBase):
    """Wraps an assert call so it carries a ``name`` in step reporting."""

    assert_: AssertCall = Field(alias="assert")

    @model_validator(mode="after")
    def _no_capture_on_assert(self):
        if self.capture:
            raise ValueError("assert: steps cannot use capture")
        return self


# ─────────────────────────────────────────────────────────────────────────────
# HTTP step (v0.4 — driven by the portal hero scenario)
# ─────────────────────────────────────────────────────────────────────────────


class HTTPExpect(BaseModel):
    """What the scenario expects to see in a single HTTP response."""

    model_config = ConfigDict(extra="forbid")

    status: int | list[int] | None = None
    body_contains: list[str] = Field(default_factory=list)
    body_not_contains: list[str] = Field(default_factory=list)
    headers_match: dict[str, str] = Field(default_factory=dict)
    body_json_equals: dict[str, Any] = Field(default_factory=dict)


class HTTPRegexCapture(BaseModel):
    """Capture a regex group out of a header or body field."""

    model_config = ConfigDict(extra="forbid")

    source: str  # e.g., "headers.location" or "body_text"
    pattern: str
    group: int = 1


class HTTPStep(_StepBase):
    """Driven HTTP request — GET/POST with expect + poll + capture.

    ``http`` is ``"GET /url"`` or ``"POST /url"``; URLs may be absolute
    or relative to ``base_url``. ``form`` and ``json`` are mutually
    exclusive. ``drain_stream`` reads-and-discards a streaming body
    (used to drive the SSE endpoint to completion so the agent runs
    to the final event before later steps poll for results).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    http: str
    base_url: str = "http://portal-self-serve:8000"
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    form: dict[str, Any] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = Field(default=None, alias="json")
    expect: HTTPExpect = Field(default_factory=HTTPExpect)
    poll: Poll | None = None
    follow_redirects: bool = False
    drain_stream: bool = False
    timeout_seconds: float = 30.0
    capture_regex: dict[str, HTTPRegexCapture] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self):
        if self.form and self.json_body is not None:
            raise ValueError("http: step cannot set both `form` and `json`")
        method, _, url = self.http.strip().partition(" ")
        if method.upper() not in ("GET", "POST"):
            raise ValueError(
                f"http: step method must be GET or POST, got {method!r}"
            )
        if not url:
            raise ValueError("http: step must include a URL after the method")
        return self


class FileReadStep(_StepBase):
    """Read a local file and capture substrings from it (v0.8).

    Used by the auth-flow hero scenarios to fetch the OTP / magic-link
    that ``LoggingEmailAdapter`` writes to the dev-mailbox file. The
    scenario YAML names the file path (interpolated against scenario
    vars) and one or more ``capture_regex`` rules. ``poll:`` retries
    while the file is missing or doesn't yet contain the pattern —
    the file may be written milliseconds after the POST that triggers
    the email send.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    file: str
    capture_regex: dict[str, HTTPRegexCapture] = Field(default_factory=dict)
    poll: Poll | None = None
    encoding: str = "utf-8"


Step = ActionStep | AskStep | AssertStep | HTTPStep | FileReadStep


# ─────────────────────────────────────────────────────────────────────────────
# Top-level scenario
# ─────────────────────────────────────────────────────────────────────────────


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    setup: Setup = Field(default_factory=Setup)
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    teardown: Teardown = Field(default_factory=Teardown)


# Mode hints used by runner + CLI flags
LLMMode = Literal["auto", "disabled", "forced"]
