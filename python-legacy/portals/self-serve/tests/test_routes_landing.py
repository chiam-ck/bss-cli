"""Public catalog browse renders plan cards from a mocked catalog client.

v0.4 mounted the plan-card landing on ``/``. v0.8 PR 5 makes ``/`` the
login-gated dashboard and moves the public plan-cards page to
``/plans``. Tests follow the move.
"""

from __future__ import annotations


def test_plans_returns_200_and_renders_three_plans(client):  # type: ignore[no-untyped-def]
    resp = client.get("/plans")
    assert resp.status_code == 200
    body = resp.text
    for plan_id in ("PLAN_S", "PLAN_M", "PLAN_L"):
        assert plan_id in body
    # Plan names from the fixture
    for name in ("Sidekick", "Mainline", "Long Haul"):
        assert name in body


def test_plans_does_not_render_agent_log_widget(client):  # type: ignore[no-untyped-def]
    """v0.11 — the Agent Activity widget was retired from the signup
    funnel (V0_11_0.md). The widget is reserved for the chat surface
    when it lands; signup pages no longer carry it."""
    resp = client.get("/plans")
    assert "Agent Activity" not in resp.text
    assert "agent-log-events" not in resp.text


def test_plans_anonymous_cta_bounces_through_login(client):  # type: ignore[no-untyped-def]
    """Anonymous viewer's CTA routes through /auth/login with `next=` set.

    Signed-in viewers go straight to the funnel — that's the
    ``authed_client`` variant below.
    """
    resp = client.get("/plans")
    for plan_id in ("PLAN_S", "PLAN_M", "PLAN_L"):
        assert f'href="/auth/login?next=/signup/{plan_id}/msisdn"' in resp.text


def test_plans_signed_in_cta_routes_to_msisdn_picker(authed_client):  # type: ignore[no-untyped-def]
    resp = authed_client.get("/plans")
    for plan_id in ("PLAN_S", "PLAN_M", "PLAN_L"):
        assert f'href="/signup/{plan_id}/msisdn"' in resp.text
