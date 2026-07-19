"""v0.16 Track 2 (redo) — stripe-mode signup tests for Stripe Checkout flow.

The original Track 2 used Stripe.js + Elements iframe; that flow failed
in real browsers (Safari ITP, HTMX swap script-execution issues). The
redo uses Stripe Checkout — full-page redirect to a Stripe-hosted card
form. These tests cover the new flow:

1. Initial /signup form drops the card-number input (PCI doctrine).
2. POST /signup accepts empty card_pan in stripe mode.
3. POST /signup/step/cof/checkout-init mints a CheckoutSession + 303
   redirects to session.url. ensure_customer is called with the right
   bss_customer_id; create_payment_method is NOT called yet.
4. GET /signup/step/cof/checkout-return retrieves the session, extracts
   the pm_*, calls create_payment_method (token_provider='stripe'),
   advances state to pending_order.
"""

from __future__ import annotations

from unittest.mock import patch


def _make_stripe_app(authed_client):
    app = authed_client.app
    prev = (
        getattr(app.state, "payment_provider", "mock"),
        getattr(app.state, "payment_stripe_api_key", ""),
    )
    app.state.payment_provider = "stripe"
    app.state.payment_stripe_api_key = "sk_test_fake_for_unit_tests"
    return prev


def _restore_app(authed_client, prev):
    app = authed_client.app
    app.state.payment_provider, app.state.payment_stripe_api_key = prev


def test_signup_form_in_stripe_mode_omits_card_number_input(authed_client):
    prev = _make_stripe_app(authed_client)
    try:
        resp = authed_client.get("/signup/PLAN_M?msisdn=90000042")
        assert resp.status_code == 200
        body = resp.text
        assert 'name="card_number"' not in body
        assert "4242424242424242" not in body
        assert "secure Stripe form" in body or "next page" in body
        assert 'name="card_pan"' in body
    finally:
        _restore_app(authed_client, prev)


def test_signup_post_in_stripe_mode_accepts_empty_card_pan(
    authed_client, fake_clients
):
    prev = _make_stripe_app(authed_client)
    try:
        resp = authed_client.post(
            "/signup",
            data={
                "plan": "PLAN_M",
                "name": "Stripe Test",
                "phone": "90000042",
                "msisdn": "90000042",
                "card_pan": "",
            },
            follow_redirects=False,
        )
        if resp.status_code == 422:
            assert "policy.payment.method.invalid_card" not in resp.text
        else:
            assert resp.status_code in (302, 303)
            assert "/signup/PLAN_M/progress" in resp.headers.get("location", "")
    finally:
        _restore_app(authed_client, prev)


def test_checkout_init_creates_session_and_redirects(
    authed_client, fake_clients
):
    """POST /signup/step/cof/checkout-init must:
    - Call PaymentClient.ensure_customer with the BSS customer_id
    - Call stripe.checkout.Session.create with mode='setup', customer=cus_*
    - 303 redirect to the session.url
    """
    prev = _make_stripe_app(authed_client)
    try:
        store = authed_client.app.state.session_store
        identity_id = authed_client.app.state.test_identity_id
        import asyncio

        async def _do() -> str:
            sig = await store.create(
                plan="PLAN_M",
                name="Stripe Test",
                email="checkout-init@example.com",
                phone="90000042",
                msisdn="90000042",
                card_pan="",
                identity_id=identity_id,
            )
            sig.customer_id = "CUST-CHECKOUT-INIT"
            sig.step = "pending_cof"
            await store.update(sig)
            return sig.session_id

        sid = asyncio.run(_do())

        # Stub bss-clients PaymentClient.ensure_customer
        ensure_args: dict = {}
        async def _fake_ensure(**kwargs):
            ensure_args.update(kwargs)
            return {
                "customer_external_ref": "cus_test_fake_001",
                "provider": "stripe",
            }
        fake_clients.payment.ensure_customer = _fake_ensure

        # Stub stripe.checkout.Session.create
        cs_create_args: dict = {}
        def _fake_cs_create(**kwargs):
            cs_create_args.update(kwargs)
            return {"id": "cs_test_fake_001", "url": "https://checkout.stripe.com/c/pay/cs_test_fake_001"}

        with patch("stripe.checkout.Session.create", side_effect=_fake_cs_create):
            resp = authed_client.post(
                f"/signup/step/cof/checkout-init?session={sid}",
                follow_redirects=False,
            )

        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_fake_001"

        # ensure_customer was called with the right BSS customer
        assert ensure_args["customer_id"] == "CUST-CHECKOUT-INIT"

        # CheckoutSession was created in the right shape
        assert cs_create_args["mode"] == "setup"
        assert cs_create_args["customer"] == "cus_test_fake_001"
        assert cs_create_args["payment_method_types"] == ["card"]
        assert "checkout-return" in cs_create_args["success_url"]
        assert "{CHECKOUT_SESSION_ID}" in cs_create_args["success_url"]
        # Metadata carries the signup session for return-trip verification
        assert cs_create_args["metadata"]["bss_signup_session"] == sid
        assert cs_create_args["metadata"]["bss_customer_id"] == "CUST-CHECKOUT-INIT"
    finally:
        _restore_app(authed_client, prev)


def test_checkout_return_registers_pm_and_advances_state(
    authed_client, fake_clients
):
    """GET /signup/step/cof/checkout-return must:
    - Retrieve the CheckoutSession from Stripe with expand=setup_intent
    - Extract setup_intent.payment_method (the pm_*)
    - Call PaymentClient.create_payment_method(token_provider='stripe', card_token=pm_*)
    - Advance state to pending_order
    """
    prev = _make_stripe_app(authed_client)
    try:
        store = authed_client.app.state.session_store
        identity_id = authed_client.app.state.test_identity_id
        import asyncio

        async def _do() -> str:
            sig = await store.create(
                plan="PLAN_M",
                name="Stripe Test",
                email="checkout-return@example.com",
                phone="90000042",
                msisdn="90000042",
                card_pan="",
                identity_id=identity_id,
            )
            sig.customer_id = "CUST-CHECKOUT-RETURN"
            sig.step = "pending_cof_elements"  # checkout in flight
            await store.update(sig)
            return sig.session_id

        sid = asyncio.run(_do())

        # Stub stripe.checkout.Session.retrieve to return a session with
        # the right metadata + an embedded setup_intent.payment_method
        def _fake_cs_retrieve(cs_id, **kwargs):
            return {
                "id": cs_id,
                "metadata": {
                    "bss_signup_session": sid,
                    "bss_customer_id": "CUST-CHECKOUT-RETURN",
                },
                "setup_intent": {
                    "id": "seti_test_fake_001",
                    "payment_method": "pm_test_fake_card_visa",
                    "status": "succeeded",
                },
            }

        # Stub create_payment_method to return a fake PM-* id
        create_pm_args: dict = {}
        async def _fake_create_pm(**kwargs):
            create_pm_args.update(kwargs)
            return {"id": "PM-CHECKOUT-RETURN-001"}
        fake_clients.payment.create_payment_method = _fake_create_pm

        with patch(
            "stripe.checkout.Session.retrieve", side_effect=_fake_cs_retrieve
        ):
            resp = authed_client.get(
                f"/signup/step/cof/checkout-return?session={sid}&cs_id=cs_test_fake_001",
                follow_redirects=False,
            )

        assert resp.status_code == 303, resp.text
        assert "/signup/PLAN_M/progress" in resp.headers["location"]

        # PaymentClient.create_payment_method was called with stripe path
        assert create_pm_args["tokenization_provider"] == "stripe"
        assert create_pm_args["card_token"] == "pm_test_fake_card_visa"
        assert create_pm_args["customer_id"] == "CUST-CHECKOUT-RETURN"

        # State advanced to pending_order
        async def _read() -> str:
            sig = await store.get(sid)
            return sig.step

        assert asyncio.run(_read()) == "pending_order"
    finally:
        _restore_app(authed_client, prev)


def test_checkout_return_rejects_metadata_mismatch(
    authed_client, fake_clients
):
    """If the CheckoutSession's metadata.bss_signup_session doesn't match
    the URL-provided session, refuse to process — defence against a
    customer pasting someone else's cs_id.
    """
    prev = _make_stripe_app(authed_client)
    try:
        store = authed_client.app.state.session_store
        identity_id = authed_client.app.state.test_identity_id
        import asyncio

        async def _do() -> str:
            sig = await store.create(
                plan="PLAN_M",
                name="Stripe Test",
                email="cs-mismatch@example.com",
                phone="90000042",
                msisdn="90000042",
                card_pan="",
                identity_id=identity_id,
            )
            sig.customer_id = "CUST-MISMATCH"
            sig.step = "pending_cof_elements"
            await store.update(sig)
            return sig.session_id

        sid = asyncio.run(_do())

        def _fake_cs_retrieve(cs_id, **kwargs):
            return {
                "id": cs_id,
                "metadata": {
                    "bss_signup_session": "DIFFERENT-SESSION-XXX",
                    "bss_customer_id": "CUST-MISMATCH",
                },
                "setup_intent": {
                    "id": "seti_xxx",
                    "payment_method": "pm_xxx",
                    "status": "succeeded",
                },
            }

        # create_payment_method MUST NOT be called.
        called = {"n": 0}
        async def _no_call(**kwargs):
            called["n"] += 1
            raise AssertionError("create_payment_method should NOT be called on metadata mismatch")
        fake_clients.payment.create_payment_method = _no_call

        with patch(
            "stripe.checkout.Session.retrieve", side_effect=_fake_cs_retrieve
        ):
            resp = authed_client.get(
                f"/signup/step/cof/checkout-return?session={sid}&cs_id=cs_xxx",
                follow_redirects=False,
            )

        # Redirects back to progress (failed state) — does not call BSS
        assert resp.status_code == 303
        assert called["n"] == 0

        async def _read():
            sig = await store.get(sid)
            return sig.step, sig.step_error
        step, err = asyncio.run(_read())
        assert step == "failed"
        assert err == "policy.payment.checkout.metadata_mismatch"
    finally:
        _restore_app(authed_client, prev)
