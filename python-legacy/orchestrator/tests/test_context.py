"""Channel/actor context tests — every call path sets the right headers."""

from __future__ import annotations

from bss_clients.base import _actor_var, _channel_var, _request_id_var
from bss_orchestrator.config import settings
from bss_orchestrator.context import (
    use_cli_context,
    use_llm_context,
    use_scenario_context,
)


def _snapshot() -> tuple[str, str, str]:
    return _actor_var.get(), _channel_var.get(), _request_id_var.get()


def test_cli_context_sets_actor_and_channel() -> None:
    rid = use_cli_context(actor="cli-user")
    actor, channel, req = _snapshot()
    assert actor == "cli-user"
    assert channel == "cli"
    assert req == rid


def test_llm_context_uses_model_derived_actor() -> None:
    use_llm_context()
    actor, channel, _ = _snapshot()
    assert channel == "llm"
    assert actor == settings.llm_actor
    # Actor slug must reflect the model, never a generic placeholder.
    assert actor.startswith("llm-")


def test_scenario_context_namespaces_actor() -> None:
    use_scenario_context(name="hero_signup")
    actor, channel, _ = _snapshot()
    assert channel == "scenario"
    assert actor == "scenario:hero_signup"


def test_request_id_is_respected_when_supplied() -> None:
    rid = use_cli_context(request_id="fixed-rid-001")
    assert rid == "fixed-rid-001"
    assert _request_id_var.get() == "fixed-rid-001"


def test_llm_actor_derived_from_model_slug() -> None:
    # Regardless of current env, the derivation must replace slashes with dashes.
    from bss_orchestrator.config import Settings

    s = Settings(llm_model="google/gemma-4-26b-a4b-it", llm_api_key="x")
    assert s.llm_actor == "llm-google-gemma-4-26b-a4b-it"
