"""Activation progress + status polling routes."""

from __future__ import annotations

import asyncio


def test_activation_redirects_to_confirmation_when_subscription_known(client):  # type: ignore[no-untyped-def]
    # Seed a session with a subscription_id as if the agent had finished.
    store = client.app.state.session_store
    sig = asyncio.run(
        store.create(
            plan="PLAN_M",
            name="n",
            email="e@x",
            phone="+0",
            msisdn="90000042",
            card_pan="4242424242424242",
        )
    )
    sig.subscription_id = "SUB-007"
    sig.order_id = "ORD-014"
    asyncio.run(store.update(sig))

    resp = client.get(
        f"/activation/ORD-014?session={sig.session_id}",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/confirmation/SUB-007?session={sig.session_id}"


def test_activation_renders_polling_shell_when_subscription_pending(client):  # type: ignore[no-untyped-def]
    store = client.app.state.session_store
    sig = asyncio.run(
        store.create(
            plan="PLAN_M",
            name="n",
            email="e@x",
            phone="+0",
            msisdn="90000042",
            card_pan="4242424242424242",
        )
    )
    sig.order_id = "ORD-014"
    # subscription_id deliberately unset
    asyncio.run(store.update(sig))

    resp = client.get(f"/activation/ORD-014?session={sig.session_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Activating" in body
    assert f"/activation/ORD-014/status?session={sig.session_id}" in body
    # The stepper partial is included initially too.
    assert "order placed" in body


def test_activation_unknown_session_404s(client):  # type: ignore[no-untyped-def]
    resp = client.get("/activation/ORD-014?session=unknown")
    assert resp.status_code == 404


def test_activation_status_hx_redirects_once_subscription_arrives(client):  # type: ignore[no-untyped-def]
    store = client.app.state.session_store
    sig = asyncio.run(
        store.create(
            plan="PLAN_M",
            name="n",
            email="e@x",
            phone="+0",
            msisdn="90000042",
            card_pan="4242424242424242",
        )
    )
    sig.subscription_id = "SUB-007"
    asyncio.run(store.update(sig))

    resp = client.get(f"/activation/ORD-014/status?session={sig.session_id}")
    assert resp.status_code == 200
    assert (
        resp.headers["hx-redirect"]
        == f"/confirmation/SUB-007?session={sig.session_id}"
    )
