"""Tests for the ``customer_self_serve`` tool profile (v0.12 PR2).

Three classes of assertion:

1. Profile registry — every name the profile lists is present in
   ``TOOL_REGISTRY``; ``validate_profiles`` raises on drift.
2. Wrapper signatures — no ``*.mine`` / ``*_for_me`` tool accepts a
   forbidden owner-bound parameter (``customer_id``, ``customer_email``,
   ``msisdn``).
3. Wrapper behaviour — wrappers refuse to run with no actor bound
   and refuse cross-customer ids when the actor is set.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from bss_orchestrator import auth_context
from bss_orchestrator.tools import TOOL_PROFILES, TOOL_REGISTRY, validate_profiles
from bss_orchestrator.tools._profiles import (
    FORBIDDEN_MINE_PARAMETERS,
    is_mine_tool,
)


# ─── 1. Profile registry integrity ──────────────────────────────────


def test_customer_self_serve_profile_exists() -> None:
    assert "customer_self_serve" in TOOL_PROFILES


def test_promo_tools_are_operator_only_never_customer_facing() -> None:
    """v1.1 doctrine: promo create/assign/show are operator/cockpit only.

    A customer types a code at checkout (order.create discount_code=) but can
    never enumerate or self-issue promotions — so no promo.* tool may appear
    in the customer_self_serve profile.
    """
    promo_tools = {t for t in TOOL_REGISTRY if t.startswith("promo.")}
    assert promo_tools, "expected promo.* tools to be registered"
    customer = TOOL_PROFILES["customer_self_serve"]
    leaked = promo_tools & customer
    assert not leaked, f"promo tools must never be customer-facing: {sorted(leaked)}"
    # and they ARE available to the operator cockpit
    assert promo_tools <= TOOL_PROFILES["operator_cockpit"]


def test_every_profile_tool_is_registered() -> None:
    for profile_name, names in TOOL_PROFILES.items():
        for name in names:
            assert name in TOOL_REGISTRY, (
                f"Profile {profile_name!r} lists {name!r} but it is "
                f"not in TOOL_REGISTRY."
            )


def test_validate_profiles_passes_at_import_time() -> None:
    # The function ran during package import; calling it again must
    # remain a no-op (no side-effects, idempotent).
    validate_profiles()


def test_validate_profiles_catches_drift_in_listed_tool() -> None:
    bogus_profile = {
        "customer_self_serve": TOOL_PROFILES["customer_self_serve"]
        | {"nonexistent.tool"}
    }
    with patch.object(
        __import__(
            "bss_orchestrator.tools._profiles", fromlist=["TOOL_PROFILES"]
        ),
        "TOOL_PROFILES",
        bogus_profile,
    ):
        with pytest.raises(RuntimeError, match="nonexistent.tool"):
            validate_profiles()


def test_validate_profiles_catches_forbidden_parameter() -> None:
    # Plant a fake mine tool that accepts ``customer_id`` and assert
    # the validator catches it.
    async def bad_mine(customer_id: str) -> dict:
        return {}

    bad_mine.__doc__ = "x"  # validate_profiles only checks signature
    with patch.dict(TOOL_REGISTRY, {"bogus.bad_mine": bad_mine}):
        with pytest.raises(RuntimeError, match="forbidden parameter"):
            validate_profiles()


# ─── 2. Wrapper signature inspection ────────────────────────────────


def _mine_tool_names() -> list[str]:
    return sorted(name for name in TOOL_REGISTRY if is_mine_tool(name))


@pytest.mark.parametrize("name", _mine_tool_names())
def test_mine_wrapper_does_not_accept_forbidden_parameters(name: str) -> None:
    fn = TOOL_REGISTRY[name]
    sig = inspect.signature(fn)
    forbidden = sorted(p for p in sig.parameters if p in FORBIDDEN_MINE_PARAMETERS)
    assert not forbidden, (
        f"{name!r} signature accepts forbidden parameter(s) {forbidden!r}. "
        "Bind from auth_context.current().actor instead."
    )


def test_at_least_one_mine_tool_is_registered() -> None:
    # Guardrail: if naming convention changed (`is_mine_tool` no
    # longer matches), the parametrize above would silently shrink to
    # zero. Pin a minimum so the suite stays meaningful.
    assert len(_mine_tool_names()) >= 8


# ─── 3. Wrapper behaviour ───────────────────────────────────────────


@pytest.fixture
def reset_actor_after():
    """Reset auth_context.actor to None after each test, regardless of outcome."""
    yield
    # No public reset; bind to None via set_actor and let GC handle it.
    token = auth_context.set_actor("__test_clear__")
    auth_context.reset_actor(token)


@pytest.mark.asyncio
async def test_subscription_list_mine_requires_actor_bound() -> None:
    from bss_orchestrator.tools.mine_wrappers import (
        _NoActorBound,
        subscription_list_mine,
    )

    with pytest.raises(_NoActorBound):
        await subscription_list_mine()


@pytest.mark.asyncio
async def test_customer_get_mine_requires_actor_bound() -> None:
    from bss_orchestrator.tools.mine_wrappers import (
        _NoActorBound,
        customer_get_mine,
    )

    with pytest.raises(_NoActorBound):
        await customer_get_mine()


@pytest.mark.asyncio
async def test_subscription_list_mine_resolves_actor_into_canonical_call(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import subscription_list_mine

    fake_clients = AsyncMock()
    fake_clients.subscription.list_for_customer = AsyncMock(return_value=[{"id": "SUB-1"}])

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await subscription_list_mine()
    finally:
        auth_context.reset_actor(token)

    # v1.1.1 — the wrapper annotates pricing; the id passes through unchanged.
    assert len(result) == 1 and result[0]["id"] == "SUB-1"
    assert "currentMonthlyCharge" in result[0]  # pricing annotation applied
    fake_clients.subscription.list_for_customer.assert_called_once_with("CUST-042")


@pytest.mark.asyncio
async def test_subscription_get_mine_rejects_cross_customer(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import (
        _NotOwnedByActor,
        subscription_get_mine,
    )

    fake_clients = AsyncMock()
    # The subscription belongs to CUST-999 — a different customer than the
    # bound actor. The wrapper must refuse before the canonical tool can
    # leak the dict.
    fake_clients.subscription.get = AsyncMock(
        return_value={"id": "SUB-7", "customerId": "CUST-999"}
    )

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            with pytest.raises(_NotOwnedByActor):
                await subscription_get_mine("SUB-7")
    finally:
        auth_context.reset_actor(token)


@pytest.mark.asyncio
async def test_subscription_get_mine_returns_owned_subscription(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import subscription_get_mine

    sub = {"id": "SUB-7", "customerId": "CUST-042", "state": "active"}
    fake_clients = AsyncMock()
    fake_clients.subscription.get = AsyncMock(return_value=sub)

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await subscription_get_mine("SUB-7")
    finally:
        auth_context.reset_actor(token)

    assert result == sub


@pytest.mark.asyncio
async def test_payment_method_list_mine_binds_actor(reset_actor_after) -> None:
    from bss_orchestrator.tools.mine_wrappers import payment_method_list_mine

    fake_clients = AsyncMock()
    fake_clients.payment.list_methods = AsyncMock(return_value=[{"id": "PM-1"}])

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await payment_method_list_mine()
    finally:
        auth_context.reset_actor(token)

    assert result == [{"id": "PM-1"}]
    fake_clients.payment.list_methods.assert_called_once_with("CUST-042")


# ─── 4. Write wrappers (PR3) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_vas_purchase_for_me_blocks_cross_customer(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import (
        _NotOwnedByActor,
        vas_purchase_for_me,
    )

    fake_clients = AsyncMock()
    fake_clients.subscription.get = AsyncMock(
        return_value={"id": "SUB-9", "customerId": "CUST-OTHER"}
    )

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            with pytest.raises(_NotOwnedByActor):
                await vas_purchase_for_me("SUB-9", "VAS_DATA_5GB")
    finally:
        auth_context.reset_actor(token)
    fake_clients.subscription.purchase_vas.assert_not_called()


@pytest.mark.asyncio
async def test_vas_purchase_for_me_passes_through_when_owned(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import vas_purchase_for_me

    fake_clients = AsyncMock()
    fake_clients.subscription.get = AsyncMock(
        return_value={"id": "SUB-9", "customerId": "CUST-042"}
    )
    fake_clients.subscription.purchase_vas = AsyncMock(
        return_value={"id": "SUB-9", "state": "active"}
    )

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await vas_purchase_for_me("SUB-9", "VAS_DATA_5GB")
    finally:
        auth_context.reset_actor(token)

    assert result == {"id": "SUB-9", "state": "active"}
    fake_clients.subscription.purchase_vas.assert_called_once_with(
        "SUB-9", "VAS_DATA_5GB"
    )


@pytest.mark.asyncio
async def test_subscription_terminate_mine_uses_chat_reason(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import subscription_terminate_mine

    fake_clients = AsyncMock()
    fake_clients.subscription.get = AsyncMock(
        return_value={"id": "SUB-7", "customerId": "CUST-042"}
    )
    fake_clients.subscription.terminate = AsyncMock(
        return_value={"id": "SUB-7", "state": "terminated"}
    )

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await subscription_terminate_mine("SUB-7")
    finally:
        auth_context.reset_actor(token)

    assert result["state"] == "terminated"
    fake_clients.subscription.terminate.assert_called_once_with(
        "SUB-7", reason="customer_chat"
    )


def test_subscription_terminate_mine_is_destructive() -> None:
    from bss_orchestrator.safety import DESTRUCTIVE_TOOLS

    assert "subscription.terminate_mine" in DESTRUCTIVE_TOOLS, (
        "The chat-side terminate wrapper must remain destructive — "
        "the operation is irreversible regardless of the wrapping."
    )


@pytest.mark.asyncio
async def test_schedule_plan_change_mine_blocks_cross_customer(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import (
        _NotOwnedByActor,
        subscription_schedule_plan_change_mine,
    )

    fake_clients = AsyncMock()
    fake_clients.subscription.get = AsyncMock(
        return_value={"id": "SUB-9", "customerId": "CUST-OTHER"}
    )

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            with pytest.raises(_NotOwnedByActor):
                await subscription_schedule_plan_change_mine("SUB-9", "PLAN_L")
    finally:
        auth_context.reset_actor(token)


@pytest.mark.asyncio
async def test_cancel_pending_plan_change_mine_idempotent_when_owned(
    reset_actor_after,
) -> None:
    from bss_orchestrator.tools.mine_wrappers import (
        subscription_cancel_pending_plan_change_mine,
    )

    fake_clients = AsyncMock()
    fake_clients.subscription.get = AsyncMock(
        return_value={"id": "SUB-9", "customerId": "CUST-042"}
    )
    fake_clients.subscription.cancel_plan_change = AsyncMock(
        return_value={"id": "SUB-9"}
    )

    token = auth_context.set_actor("CUST-042")
    try:
        with patch(
            "bss_orchestrator.tools.mine_wrappers.get_clients",
            return_value=fake_clients,
        ):
            result = await subscription_cancel_pending_plan_change_mine("SUB-9")
    finally:
        auth_context.reset_actor(token)

    assert result == {"id": "SUB-9"}
    fake_clients.subscription.cancel_plan_change.assert_called_once_with("SUB-9")
