"""Signup route — form render + POST /signup direct-write step 1 (v0.11+).

v0.11 migrates the signup chain from orchestrator-mediated (one
``astream_once`` call streaming every tool through SSE) to deterministic
direct-API calls. The POST /signup handler now runs ``crm.create_customer``
+ ``link_to_customer`` directly, then redirects to the progress page,
which fires the rest of the chain via HTMX (``/signup/step/{kyc,cof,
order,poll}``). Tests for the chained step routes live in
``test_signup_funnel_v0_8.py``; this file covers the form + POST.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse


def test_signup_form_renders_plan_details_without_misleading_kyc_badge(authed_client):  # type: ignore[no-untyped-def]
    resp = authed_client.get("/signup/PLAN_M?msisdn=90000042")
    assert resp.status_code == 200
    body = resp.text
    assert "PLAN_M" in body
    assert "Mainline" in body
    # Chosen MSISDN threads through the form
    assert "90000042" in body
    assert "+65 9000 0042" in body  # display-formatted
    # v0.15: the misleading "✓ Identity verified via Myinfo (simulated)"
    # badge has been removed from the order form. KYC happens AFTER form
    # submit at /signup/step/kyc, not before.
    assert "KYC-PREBAKED-001" not in body
    assert "Identity verified via Myinfo" not in body
    # Mock tokenizer test card is the default
    assert "4242424242424242" in body


def test_signup_form_without_msisdn_query_param_422s(authed_client):  # type: ignore[no-untyped-def]
    # Direct hit on /signup/{plan} without choosing a number first is a
    # programmer / scraper error; the form has nothing to render.
    resp = authed_client.get("/signup/PLAN_M")
    assert resp.status_code == 422


def test_authed_pages_show_logout_form_in_nav(authed_client):  # type: ignore[no-untyped-def]
    """Every authed page renders a sign-out POST in the header so a
    logged-in visitor can always log out without hunting for the
    dashboard. Pre-v0.8.1 the only logout entry point was the dashboard
    sign-out button, which the operator complained about."""
    resp = authed_client.get("/signup/PLAN_M?msisdn=90000042")
    assert resp.status_code == 200
    body = resp.text
    assert 'action="/auth/logout"' in body
    assert "sign out" in body.lower()
    # Email pill in the nav makes it obvious which account is signed in.
    assert "ada@example.sg" in body
    # Stale "unauthenticated" footer copy is gone.
    assert "unauthenticated" not in body


def test_signup_form_does_not_ask_for_email_again(authed_client):  # type: ignore[no-untyped-def]
    """v0.8: signup-form must read the email from the verified session
    instead of letting the visitor type it again. Letting it be
    re-entered creates a CRM customer with a different address than
    the linked identity — silent data divergence between
    portal_auth.identity and crm.contact_medium.
    """
    resp = authed_client.get("/signup/PLAN_M?msisdn=90000042")
    assert resp.status_code == 200
    body = resp.text
    # No email input on the form.
    assert 'name="email"' not in body
    assert 'type="email"' not in body
    # The verified email is shown as a read-only banner instead.
    assert "Signed in as" in body
    assert "ada@example.sg" in body  # matches authed_client's seeded identity


def test_signup_post_uses_session_email_not_form_email(authed_client, fake_clients):  # type: ignore[no-untyped-def]
    """Even if a form-side email is submitted (e.g. by a hand-crafted
    POST), the route must use identity.email from the verified session.
    """
    fake_clients.crm.next_customer_id = "CUST-901"
    resp = authed_client.post(
        "/signup",
        data={
            "plan": "PLAN_M",
            "name": "Ada",
            # Hostile / outdated client tries to forge a different email.
            "email": "evil@attacker.example",
            "phone": "+6590001234",
            "msisdn": "90000042",
            "card_pan": "4242424242424242",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # v0.11 — POST /signup runs crm.create_customer directly. Verify the
    # call was made with identity.email, not the attacker-supplied email.
    assert len(fake_clients.crm.create_customer_calls) == 1
    call = fake_clients.crm.create_customer_calls[0]
    assert call["email"] == "ada@example.sg"
    assert call["email"] != "evil@attacker.example"


def test_signup_unknown_plan_returns_404(authed_client):  # type: ignore[no-untyped-def]
    resp = authed_client.get("/signup/PLAN_NOPE?msisdn=90000042")
    assert resp.status_code == 404


def test_signup_post_runs_create_customer_and_redirects_to_progress(  # type: ignore[no-untyped-def]
    authed_client, fake_clients
):
    """v0.11 — POST /signup is one BSS write (crm.create_customer) plus a
    portal-auth link_to_customer. Redirect to progress; no SSE stream."""
    fake_clients.crm.next_customer_id = "CUST-042"
    resp = authed_client.post(
        "/signup",
        data={
            "plan": "PLAN_M",
            "name": "Ada Lovelace",
            "phone": "+6590001234",
            "msisdn": "90000042",
            "card_pan": "4242424242424242",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/signup/PLAN_M/progress?session=")
    session_id = parse_qs(urlparse(location).query)["session"][0]
    assert len(session_id) == 32

    # The chosen number + identity.email stuck in the session.
    import asyncio
    store = authed_client.app.state.session_store
    sig = asyncio.run(store.get(session_id))
    assert sig is not None
    assert sig.msisdn == "90000042"
    assert sig.email == "ada@example.sg"
    assert sig.customer_id == "CUST-042"
    # Step 1 done; chain ready to advance to KYC on the progress page.
    assert sig.step == "pending_kyc"

    # Exactly one create_customer call.
    assert len(fake_clients.crm.create_customer_calls) == 1


def test_signup_progress_page_renders_timeline_without_sse(  # type: ignore[no-untyped-def]
    authed_client, fake_clients
):
    """Progress page is now an HTMX-driven 5-step timeline. No
    sse-connect on the body; no agent log stream attached."""
    fake_clients.crm.next_customer_id = "CUST-042"
    resp = authed_client.post(
        "/signup",
        data={
            "plan": "PLAN_M",
            "name": "Ada Lovelace",
            "phone": "+6590001234",
            "msisdn": "90000042",
            "card_pan": "4242424242424242",
        },
        follow_redirects=False,
    )
    location = resp.headers["location"]
    progress = authed_client.get(location)
    assert progress.status_code == 200
    body = progress.text
    # Timeline labels appear; the next-step trigger fires the chain.
    assert "create customer" in body
    assert "attest KYC" in body
    assert "place order" in body
    # No SSE wiring on the progress page.
    assert "sse-connect" not in body
    assert "/agent/events/" not in body


def test_signup_progress_with_unknown_session_404s(authed_client):  # type: ignore[no-untyped-def]
    resp = authed_client.get("/signup/PLAN_M/progress?session=unknown")
    assert resp.status_code == 404


def test_signup_post_renders_structured_error_on_policy_violation(  # type: ignore[no-untyped-def]
    authed_client, fake_clients
):
    """A PolicyViolationFromServer on crm.create_customer re-renders the
    signup form with a customer-facing error, not a 500."""
    from bss_clients import PolicyViolationFromServer

    fake_clients.crm.next_error = PolicyViolationFromServer(
        rule="customer.create.email_unique",
        message="Email already belongs to another customer.",
    )
    resp = authed_client.post(
        "/signup",
        data={
            "plan": "PLAN_M",
            "name": "Ada",
            "phone": "+6590001234",
            "msisdn": "90000042",
            "card_pan": "4242424242424242",
        },
        follow_redirects=False,
    )
    # 422 + form re-rendered with the structured error message.
    assert resp.status_code == 422
    assert "PLAN_M" in resp.text
