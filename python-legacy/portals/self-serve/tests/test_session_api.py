"""JSON session status endpoint — the scenario runner polls this."""

from __future__ import annotations


def test_session_api_returns_404_for_unknown(client):  # type: ignore[no-untyped-def]
    assert client.get("/api/session/unknown").status_code == 404


def test_session_api_returns_projection(client):  # type: ignore[no-untyped-def]
    import asyncio
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
    sig.customer_id = "CUST-042"
    sig.order_id = "ORD-014"
    sig.subscription_id = "SUB-007"
    sig.activation_code = "LPA:1$smdp.example$abc"
    sig.done = True
    asyncio.run(store.update(sig))

    resp = client.get(f"/api/session/{sig.session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["done"] is True
    assert body["subscription_id"] == "SUB-007"
    assert body["order_id"] == "ORD-014"
    assert body["customer_id"] == "CUST-042"
    assert body["activation_code"].startswith("LPA:1$")
    # Never leaks raw PAN
    assert "card_pan" not in body
