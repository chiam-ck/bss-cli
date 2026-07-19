"""Output ownership trip-wire — defence-in-depth for the chat surface (v0.12).

Doctrine: server-side policies are the primary boundary. The wrappers in
``tools/mine_wrappers.py`` add a second pre-flight check at the prompt
boundary. This module is the third layer — a check on the *output*
side that fires after every tool result.

Rationale: if a tool ever returns a row whose ``customerId`` does not
match the chat's bound actor, that is a P0 — either the policy missed
the case, the wrapper's pre-check missed an alias, or the canonical
tool returned more data than its contract advertises. We want to fail
loudly the day that happens, not ship the leak to the customer.

The check is a trip-wire, not the gate. ``OWNERSHIP_PATHS`` enumerates
the JSON paths whose values must equal the actor for each
customer-bound tool. Tools not listed default to "no customer-bound
output" (the conservative read of the phase doc — they don't trip).

The startup self-check (``validate_profiles`` in ``tools/_profiles.py``)
asserts every tool in the ``customer_self_serve`` profile has an entry
in ``OWNERSHIP_PATHS`` (even if empty), so adding a profile tool
without thinking about its ownership shape fails at deploy time.

Tripping behaviour:

* The orchestrator emits an ``AgentEventError`` so ``astream_once``
  terminates the stream cleanly.
* The route handler catches and renders a generic
  "Sorry, I couldn't complete that" — no leaked detail.
* A CRM interaction row is logged on the actor's record with the
  full violation payload so CSR / ops can investigate (this also
  emits an ``audit.domain_event`` server-side per the v0.1 CRM
  auto-logging doctrine).
* structlog emits a high-severity ``agent.ownership_violation``
  log event for monitoring pipelines.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class AgentOwnershipViolation(Exception):
    """Raised when a tool's response leaks a customer-bound id that
    does not match the chat's bound actor.

    Carries the tool name, expected actor, and the offending value(s)
    for audit-row payload construction.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        actor: str,
        path: str,
        found: Any,
    ) -> None:
        self.tool_name = tool_name
        self.actor = actor
        self.path = path
        self.found = found
        super().__init__(
            f"agent.ownership_violation: tool {tool_name!r} returned "
            f"{path}={found!r} for actor {actor!r}"
        )


# JSON paths in each tool's response that must equal the bound actor.
#
# Path syntax (kept tiny — no jsonpath-ng):
#   "key"            walk the top-level dict key
#   "a.b"            walk a top-level dict's nested key
#   "[*].key"        every element of the top-level list, walk ``key``
#   "[*].a.b"        list of dicts, then nested key
#
# An empty list means "this tool's response carries no customer-bound
# fields by contract" — the entry must still be present so the startup
# self-check can confirm we thought about it.
OWNERSHIP_PATHS: dict[str, list[str]] = {
    # Public catalog reads — no customer-bound output.
    "catalog.list_vas": [],
    "catalog.list_active_offerings": [],
    "catalog.get_offering": [],
    # Subscription reads.
    "subscription.list_mine": ["[*].customerId"],
    "subscription.get_mine": ["customerId"],
    # Balances are scoped by subscription_id, which the wrapper has
    # already ownership-checked. The response itself does not carry
    # customerId — wrapping the trip-wire would require the canonical
    # tool to surface it, and we do not want to regress the canonical
    # contract for an output check that is already gated by the
    # wrapper's pre-flight.
    "subscription.get_balance_mine": [],
    "subscription.get_lpa_mine": [],
    # Usage history ditto — keyed on subscription_id which the wrapper
    # asserted owned. No customerId in the response.
    "usage.history_mine": [],
    # Customer / payment reads.
    "customer.get_mine": ["id"],
    "payment.method_list_mine": ["[*].customerId"],
    "payment.charge_history_mine": ["[*].customerId"],
    # Writes return the affected aggregate — its customerId must match.
    "vas.purchase_for_me": ["customerId"],
    "subscription.schedule_plan_change_mine": ["customerId"],
    "subscription.cancel_pending_plan_change_mine": ["customerId"],
    "subscription.terminate_mine": ["customerId"],
    # PR6 — the case-open response carries the actor's customerId.
    "case.open_for_me": ["customerId"],
    # v0.13.1 — case.list_for_me returns a list of cases, each with
    # the actor's customerId. The wrapper passes customer_id from
    # auth_context, but the trip-wire still asserts on the response
    # shape so a future server-side bug returning another customer's
    # row is caught.
    "case.list_for_me": ["[*].customerId"],
}


def _walk(obj: Any, path: str) -> list[tuple[str, Any]]:
    """Resolve ``path`` against ``obj`` and return ``(label, value)``
    tuples for every leaf reached.

    Returns an empty list when the path does not exist (a missing key
    is not, in itself, an ownership violation — the canonical tool's
    contract may legitimately omit the key in some responses).
    """
    parts = path.split(".")
    frontier: list[tuple[str, Any]] = [("", obj)]
    for part in parts:
        next_frontier: list[tuple[str, Any]] = []
        for label, value in frontier:
            if part == "[*]":
                if not isinstance(value, list):
                    continue
                for i, elem in enumerate(value):
                    next_frontier.append((f"{label}[{i}]", elem))
            else:
                if not isinstance(value, dict):
                    continue
                if part not in value:
                    continue
                new_label = f"{label}.{part}" if label else part
                next_frontier.append((new_label, value[part]))
        frontier = next_frontier
    return frontier


def assert_owned_output(
    *,
    tool_name: str,
    result_json: str,
    actor: str,
) -> None:
    """Trip-wire: raise if a customer-bound field in ``result_json``
    does not equal ``actor``.

    Args:
        tool_name: dotted tool name as registered.
        result_json: the tool's serialised response (the same string
            the LLM observed). Non-JSON results are tolerated — they
            cannot carry customer-bound fields by definition.
        actor: the chat session's bound customer_id.

    Raises:
        AgentOwnershipViolation: a configured path resolved to a value
            that did not match ``actor``.
    """
    paths = OWNERSHIP_PATHS.get(tool_name)
    if paths is None:
        # Unconfigured tool: conservative — do not trip. The startup
        # self-check enforces that every customer_self_serve profile
        # tool has an entry, so an unknown name here is a tool the
        # chat surface is not configured to call. Belt and braces.
        return
    if not paths:
        return
    try:
        parsed = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        # Tool returned a non-JSON observation (e.g. a tool-error
        # string). Cannot carry a customer-bound field; nothing to check.
        return

    for path in paths:
        for label, value in _walk(parsed, path):
            if value != actor:
                raise AgentOwnershipViolation(
                    tool_name=tool_name,
                    actor=actor,
                    path=label or path,
                    found=value,
                )


async def record_violation(
    *,
    crm_client: Any,
    actor: str,
    tool_name: str,
    path: str,
    found: Any,
    transcript_so_far: str = "",
) -> None:
    """Best-effort: log to structlog + emit a CRM interaction on the
    actor's record so the violation is auditable.

    The CRM interaction triggers the v0.1 auto-logging path which
    writes an ``audit.domain_event`` row server-side, satisfying the
    "audit row written" requirement from phases/V0_12_0.md §2.1.

    Failures here must not mask the original violation; they're
    logged and swallowed.
    """
    body = (
        f"Tool: {tool_name}\n"
        f"Path: {path}\n"
        f"Found value: {found!r}\n"
        f"Expected actor: {actor}\n"
        f"Transcript (first 1000 chars):\n{transcript_so_far[:1000]}"
    )
    logger.error(
        "agent.ownership_violation",
        tool_name=tool_name,
        actor=actor,
        path=path,
        found=str(found)[:200],
    )
    try:
        await crm_client.log_interaction(
            customer_id=actor,
            summary=(
                f"P0 agent ownership violation on {tool_name!r} — "
                f"output leaked {path}={found!r}"
            ),
            body_text=body,
            direction="outbound",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.error(
            "agent.ownership_violation.audit_log_failed",
            tool_name=tool_name,
            actor=actor,
            error=str(exc),
        )


def validate_ownership_paths_cover_profile(profile_tools: set[str]) -> None:
    """Startup self-check: every tool in ``profile_tools`` has an
    entry in ``OWNERSHIP_PATHS``.

    An entry of ``[]`` is allowed (means "no customer-bound output by
    contract"). What is *not* allowed is a missing entry — the seam
    must be deliberate.

    Raises:
        RuntimeError: a profile tool has no OWNERSHIP_PATHS entry.
    """
    missing = sorted(t for t in profile_tools if t not in OWNERSHIP_PATHS)
    if missing:
        raise RuntimeError(
            f"OWNERSHIP_PATHS is missing entries for {missing!r}. "
            f"Every tool in the customer_self_serve profile needs an "
            f"explicit entry — use [] if the tool's response carries "
            f"no customer-bound fields, but be deliberate about it."
        )
