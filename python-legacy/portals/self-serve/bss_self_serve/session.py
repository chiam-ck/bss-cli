"""In-memory signup session store.

A signup spans many requests: the POST that runs ``customer.create``,
the GET that lands on the progress page, and the HTMX-triggered step
routes that run the rest of the chain (``customer.attest_kyc``,
``payment.add_card``, ``com.create_order``, ``com.get_order`` polling).
They all share form input + the per-step results (CUST-id, payment
method id, order id, subscription id, activation code).

Production portals would put this in Redis. v0.4 was a demo — one
process, one dict, TTL-bounded. v0.11 keeps the same posture for the
direct-write signup chain because the demo invariant is identical: if
the process restarts mid-signup the user loses their session, and
that's still acceptable per V0_4_0.md §3.

Thread/async safety: protected by a single ``asyncio.Lock`` so
``create_session`` / ``get_session`` / ``update_session`` can be
called from route handlers without race-in-place on the dict. No
cross-process sharing; scaling beyond one replica requires swapping
this module for Redis.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

# v0.11 — explicit step states for the direct-write chain. The progress
# page reads ``step`` to render the timeline and to decide which step
# route to fire next. The poll route flips it to ``completed`` when
# ``com.get_order`` returns ``state == completed``; any policy violation
# along the chain flips it to ``failed`` and stores ``step_error``.
SignupStep = Literal[
    "pending_customer",     # before POST /signup runs customer.create
    "pending_kyc",          # CUST-id known; next call is customer.attest_kyc
    "pending_kyc_handoff",  # v0.15 — Didit hosted UI active; polling for the corroborating webhook
    "pending_cof",          # KYC done; next call is payment.add_card (mock auto-tokenize OR mount Stripe Elements)
    "pending_cof_elements", # v0.16 — Stripe.js + Elements iframe mounted, waiting for customer to enter card
    "pending_order",        # COF added; next call is com.create_order + submit
    "pending_activation",   # order placed; polling com.get_order until completed
    "completed",            # subscription active; activation code known
    "failed",               # any step raised a structured error; see step_error
]


@dataclass
class SignupSession:
    """All state for one in-flight signup.

    ``card_pan`` is held in memory for the short window between form
    submission and ``payment.add_card`` running through the mock
    tokenizer. We never persist it to disk — TTL-bounded RAM only —
    and the redacted ``card_pan_last4`` is what any log line or
    template ever sees. Production would tokenize at the edge and
    never hand the raw PAN to the portal process at all.
    """

    session_id: str
    plan: str
    name: str
    email: str
    phone: str
    msisdn: str  # chosen on the picker page; passed to order.create as msisdn_preference
    card_pan: str  # in-memory only, cleared once the chain finishes
    card_pan_last4: str
    # v1.1 — optional typed promo code entered on the signup form. Carried to
    # com.create_order(discount_code=) at the order step. Empty string = none;
    # an invalid code never blocks signup (catalog validates, order proceeds at
    # full price).
    promo_code: str = ""
    # v1.1 — customer unticked their auto-applied assigned offer in the funnel.
    # Passed to com.create_order(skip_assigned_offer=) at the order step.
    skip_assigned_offer: bool = False
    # v0.8 — portal-auth identity that owns this signup. Pinned at the
    # POST /signup step from request.state.identity (the verified-email
    # session). The POST handler calls ``link_to_customer(identity_id,
    # customer_id)`` the moment ``customer.create`` returns CUST-* so
    # the binding survives even if the customer abandons mid-flow.
    identity_id: str | None = None
    created_at: float = field(default_factory=monotonic)

    # v0.11 — explicit step state for the direct-write chain.
    step: SignupStep = "pending_customer"
    step_error: str | None = None  # PolicyViolation rule string, when step=="failed"
    # v0.11 — when the poll route first detects completion it renders
    # the "all 5 ticks ✓ Activated" fragment AND arms a 1.5s delayed
    # re-trigger so the user sees the chain finish before HX-Redirect
    # whisks them to /confirmation. The next poll visit, with this
    # flag already true, is the one that emits the redirect.
    redirect_armed: bool = False

    # Populated as each step completes:
    customer_id: str | None = None
    payment_method_id: str | None = None
    order_id: str | None = None
    subscription_id: str | None = None
    activation_code: str | None = None
    error: str | None = None
    done: bool = False

    # v0.15 — when the portal-side KYC adapter is Didit, the customer
    # leaves the portal to complete verification on Didit's hosted UI.
    # We stash the provider's session id here so the callback handler
    # at /signup/step/kyc/callback can fetch the attestation without
    # trusting query string.
    kyc_provider_session_id: str | None = None
    # v0.15 — the Didit hosted-UI URL (and matching QR data URI) shown
    # to the customer during pending_kyc_handoff. Populated by
    # POST /signup/step/kyc and read by the progress fragment.
    kyc_verify_url: str | None = None
    kyc_verify_qr: str | None = None

    # v0.11 — historical shape preserved (the confirmation page renders
    # ``event_log`` if it's non-empty for back-compat with v0.10 git tag
    # replays). The direct-write chain does NOT populate this list; it
    # stays empty, and the confirmation page falls back to a static
    # 5-step summary derived from the captured ids.
    event_log: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""


class SessionStore:
    """TTL-bounded in-memory dict of SignupSession keyed by session_id."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, SignupSession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        plan: str,
        name: str,
        email: str,
        phone: str,
        msisdn: str,
        card_pan: str,
        identity_id: str | None = None,
        promo_code: str = "",
        skip_assigned_offer: bool = False,
    ) -> SignupSession:
        session_id = uuid.uuid4().hex
        session = SignupSession(
            session_id=session_id,
            plan=plan,
            name=name,
            email=email,
            phone=phone,
            msisdn=msisdn,
            card_pan=card_pan,
            card_pan_last4=card_pan[-4:],
            identity_id=identity_id,
            promo_code=promo_code.strip(),
            skip_assigned_offer=skip_assigned_offer,
        )
        async with self._lock:
            self._prune_locked()
            self._items[session_id] = session
        return session

    async def get(self, session_id: str) -> SignupSession | None:
        async with self._lock:
            self._prune_locked()
            return self._items.get(session_id)

    async def update(self, session: SignupSession) -> None:
        async with self._lock:
            self._items[session.session_id] = session

    def _prune_locked(self) -> None:
        cutoff = monotonic() - self._ttl
        expired = [sid for sid, s in self._items.items() if s.created_at < cutoff]
        for sid in expired:
            self._items.pop(sid, None)
