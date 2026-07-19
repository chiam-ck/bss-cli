"""Channel/actor context for outbound bss-clients calls.

Every HTTP call from the CLI or orchestrator propagates three headers so the
downstream services (CRM's interaction auto-logging in particular) know who
did what through which surface:

    X-BSS-Actor    — human user or LLM model slug
    X-BSS-Channel  — ``cli`` | ``llm`` | ``scenario``
    X-Request-ID   — request correlation ID

This module wraps ``bss_clients.set_context`` in two convenience helpers:

    use_cli_context()   → channel=cli,  actor=cli-user
    use_llm_context()   → channel=llm,  actor=llm-<model-slug>

Phase 10's scenario runner will add ``use_scenario_context(name)``.
"""

from __future__ import annotations

import uuid

from bss_clients import set_context

from .config import settings


def use_cli_context(*, actor: str = "cli-user", request_id: str | None = None) -> str:
    """Mark subsequent bss-clients calls as originating from the direct CLI.

    Returns the request_id so the caller can log/echo it for troubleshooting.
    """
    rid = request_id or str(uuid.uuid4())
    set_context(actor=actor, channel="cli", request_id=rid)
    return rid


def use_llm_context(*, request_id: str | None = None) -> str:
    """Mark subsequent bss-clients calls as originating from the LLM.

    Actor is derived from ``settings.llm_actor`` (e.g. ``llm-google-gemma-4-26b-a4b-it``)
    so audit rows reflect which model performed the action.
    """
    rid = request_id or str(uuid.uuid4())
    set_context(actor=settings.llm_actor, channel="llm", request_id=rid)
    return rid


def use_scenario_context(
    *, name: str, request_id: str | None = None
) -> str:
    """Mark calls as originating from a scenario runner (Phase 10)."""
    rid = request_id or str(uuid.uuid4())
    set_context(actor=f"scenario:{name}", channel="scenario", request_id=rid)
    return rid


def use_channel_context(
    *, channel: str, actor: str | None = None, request_id: str | None = None
) -> str:
    """Mark calls with an arbitrary channel label — used by portals (v0.4+).

    When a portal (self-serve, CSR) routes a request through the LLM, it
    sets ``channel="portal-self-serve"`` so CRM's interaction log attributes
    the resulting actions to the portal, not to raw LLM use. Actor stays
    the model slug by default so forensic "which model did this" lookups
    still work via ``audit.domain_event.actor``.
    """
    rid = request_id or str(uuid.uuid4())
    set_context(
        actor=actor or settings.llm_actor,
        channel=channel,
        request_id=rid,
    )
    return rid
