"""Confirmation page — QR PNG + LPA code + plan summary (v0.11).

v0.4 rendered an agent-log transcript replay on this page. v0.11
retired the agent log from signup (CLAUDE.md (v0.11+ / chat only));
the page is now a clean post-activation summary: subscription id,
QR + LPA code, plan details, "create another" CTA.
"""

from __future__ import annotations


def _seed_session_and_subscription(client, fake_clients, *, with_activation_on_session=True):  # type: ignore[no-untyped-def]
    import asyncio
    store = client.app.state.session_store
    sig = asyncio.run(
        store.create(
            plan="PLAN_M",
            name="Ada",
            email="ada@example.sg",
            phone="+6590001234",
            msisdn="90000042",
            card_pan="4242424242424242",
        )
    )
    sig.subscription_id = "SUB-007"
    sig.order_id = "ORD-014"
    if with_activation_on_session:
        sig.activation_code = "LPA:1$smdp.bss-cli.local$abc-123-def"
    sig.done = True
    asyncio.run(store.update(sig))

    fake_clients.subscription.records["SUB-007"] = {
        "id": "SUB-007",
        "state": "active",
        "offeringId": "PLAN_M",
        "iccid": "8910101000000000001",
        "msisdn": "90000001",
    }
    return sig


def test_confirmation_shows_qr_lpa_and_plan(client, fake_clients):  # type: ignore[no-untyped-def]
    sig = _seed_session_and_subscription(client, fake_clients)
    resp = client.get(f"/confirmation/SUB-007?session={sig.session_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "SUB-007" in body
    assert "LPA:1$smdp.bss-cli.local$abc-123-def" in body
    # PNG QR embedded as a data URI
    assert 'src="data:image/png;base64,' in body
    # Plan details rendered from the catalog (20480 mb → 20 GB in fixture)
    assert "Mainline" in body
    assert "20 GB" in body


def test_confirmation_falls_back_to_inventory_when_session_missing_lpa(client, fake_clients):  # type: ignore[no-untyped-def]
    sig = _seed_session_and_subscription(client, fake_clients, with_activation_on_session=False)
    fake_clients.inventory.activations["8910101000000000001"] = {
        "iccid": "8910101000000000001",
        "activation_code": "LPA:1$smdp.bss-cli.local$fallback-xyz",
    }

    resp = client.get(f"/confirmation/SUB-007?session={sig.session_id}")
    assert resp.status_code == 200
    assert "LPA:1$smdp.bss-cli.local$fallback-xyz" in resp.text


def test_confirmation_unknown_session_404s(client, fake_clients):  # type: ignore[no-untyped-def]
    resp = client.get("/confirmation/SUB-007?session=unknown")
    assert resp.status_code == 404


def test_confirmation_does_not_render_agent_log_widget(client, fake_clients):  # type: ignore[no-untyped-def]
    """v0.11 — confirmation page is a clean summary, no agent log."""
    sig = _seed_session_and_subscription(client, fake_clients)
    resp = client.get(f"/confirmation/SUB-007?session={sig.session_id}")
    body = resp.text
    assert "Agent Activity" not in body
    # Defense-in-depth: no SSE wiring.
    assert "sse-connect" not in body
    assert "sse-swap" not in body
