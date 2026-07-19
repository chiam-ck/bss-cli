"""Login-gated `/` — customer dashboard (v0.10 fills the placeholder).

History:

* v0.4 mounted ``/`` as the anonymous plan-cards landing.
* v0.8 made ``/`` the login-gated dashboard with two states (unlinked
  empty / linked placeholder).
* v0.10 fills in the linked path: list the customer's subscriptions
  with state-aware CTAs, balance bars, days remaining, and a pending
  plan-change banner where applicable.

Doctrine (V0_10_0.md Track 2):

* Reads only — the dashboard does not write. ``customer_id`` is bound
  from ``request.state.customer_id`` (verified session); the call to
  ``subscription.list_for_customer`` is server-side scoped by that id,
  so cross-customer reads are not possible from this route.
* No orchestrator. The reads go directly via the v0.9
  ``NamedTokenAuthProvider`` clients — same pattern v0.8 used for the
  placeholder. ``audit.domain_event`` rows recorded on writes (which
  this route does not perform) carry ``service_identity =
  "portal_self_serve"`` via the named token.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from bss_models import discount_label
from bss_portal_auth import IdentityView, SessionView
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..clients import get_clients
from ..security import requires_session
from ..templating import templates

log = structlog.get_logger(__name__)

router = APIRouter()


# Allowance type → label shown on the line card. Lowercase keys
# match the BSS subscription.balance response.
ALLOWANCE_LABEL = {
    "data": "Data",
    "voice": "Voice",
    "sms": "SMS",
    # v0.17 — additive roaming bucket; rendered only when the offering
    # carries a non-zero quota OR the customer has topped up via
    # VAS_ROAMING_1GB. The filter sits in `_line_view` so the template
    # stays a flat for-loop over `bars`.
    "data_roaming": "Roaming",
}


def _bar_for(balance: dict[str, Any]) -> dict[str, Any]:
    """Compute the proportional fill for a single allowance.

    Mirrors the v0.6 ASCII bar's intent (proportional, not flat
    blocks). Returns a dict the template renders as a CSS-gradient
    width: ``percent`` is a 0..100 integer, ``low`` flips the bar's
    visual treatment under 10%, and ``unlimited`` uses a dashed-fill
    rendering for ``total = -1``.
    """
    total = int(balance.get("total", 0))
    consumed = int(balance.get("consumed", 0))
    remaining = int(balance.get("remaining", max(total - consumed, 0)))

    if total < 0:
        return {
            "label": ALLOWANCE_LABEL.get(balance.get("allowanceType", ""), "?"),
            "unit": balance.get("unit", ""),
            "remaining": remaining,
            "total": total,
            "percent": 100,
            "unlimited": True,
            "low": False,
            "exhausted": False,
        }

    percent = 0
    if total > 0:
        # Proportional fill — same intent as v0.6 progress_bar's
        # sub-cell resolution. We round to the nearest integer
        # percent so the CSS width attribute is small + greppable.
        percent = max(0, min(100, round((remaining / total) * 100)))

    return {
        "label": ALLOWANCE_LABEL.get(balance.get("allowanceType", ""), "?"),
        "unit": balance.get("unit", ""),
        "remaining": remaining,
        "total": total,
        "percent": percent,
        "unlimited": False,
        "low": 0 < percent <= 10,
        "exhausted": remaining <= 0 and total > 0,
    }


def _days_remaining(period_end: str | None, now_iso: str) -> int | None:
    """Days from ``now`` to ``period_end`` (positive int, 0, or None).

    Both inputs are ISO-8601 strings (BSS service responses serialise
    timestamps as ISO via Pydantic). Returns None if ``period_end`` is
    unset (e.g. pending_activation lines have no current period yet).
    """
    if not period_end:
        return None
    from datetime import datetime

    end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    delta = end - now
    return max(0, delta.days)


def _cta_for(state: str, has_pending_plan_change: bool) -> str:
    """Pick the dashboard CTA branch label.

    Branches map 1:1 to the V0_10_0.md Track 2.1 list. The template
    consumes this string to render the right button block; we keep
    the branch logic in Python so adding a new state is a single
    Python edit + a single template block, not three.
    """
    if state == "active" and has_pending_plan_change:
        return "pending_plan_change"
    if state == "blocked":
        return "blocked"
    if state == "pending_activation":
        return "pending_activation"
    if state == "terminated":
        return "terminated"
    return "active"


async def _line_view(
    sub: dict[str, Any],
    *,
    offering_name: str,
    now_iso: str,
    balances: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose the per-line dict the line_card template consumes."""
    state = sub.get("state", "")
    has_pending = sub.get("pendingOfferingId") is not None
    # v0.17 — hide the Roaming bar when the plan includes no roaming
    # AND the customer has not topped up. Plans with explicit zero
    # roaming (e.g. PLAN_S/BA_S_ROAM=0) materialize the BundleBalance
    # row but render with total=0/remaining=0 — those should NOT show
    # up as a stranded "Roaming 0/0" bar.
    bars = [_bar_for(b) for b in balances]
    bars = [
        b
        for b in bars
        if b.get("label") != "Roaming"
        or b.get("total", 0) > 0
        or b.get("remaining", 0) > 0
    ]
    # v1.1 — applied promo discount, read straight off the subscription row
    # (authoritative for what's actually charged this period). Rendered as a
    # badge on the line card. remaining == -1 → perpetual.
    discount = None
    if sub.get("discountType"):
        remaining = sub.get("discountPeriodsRemaining", 0)
        discount = {
            "label": discount_label(
                sub.get("discountType"),
                sub.get("discountValue") or 0,
                sub.get("priceCurrency", "SGD"),
            ),
            "effective_amount": sub.get("effectiveAmount"),
            "base_amount": sub.get("priceAmount"),
            "periods_remaining": remaining,
            "perpetual": remaining == -1,
            "active": remaining != 0,
        }
    return {
        "id": sub["id"],
        "msisdn": sub.get("msisdn", ""),
        "state": state,
        "state_label": state.replace("_", " ").upper(),
        "offering_id": sub.get("offeringId", ""),
        "offering_name": offering_name,
        "current_period_end": sub.get("currentPeriodEnd"),
        "next_renewal_at": sub.get("nextRenewalAt"),
        "terminated_at": sub.get("terminatedAt"),
        "days_remaining": _days_remaining(sub.get("currentPeriodEnd"), now_iso),
        "pending_offering_id": sub.get("pendingOfferingId"),
        "pending_effective_at": sub.get("pendingEffectiveAt"),
        "cta_branch": _cta_for(state, has_pending),
        "bars": bars,
        "discount": discount,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _session: SessionView = Depends(requires_session),
) -> HTMLResponse:
    """Login-gated dashboard.

    Branches on the linked-customer state:

    * No session                      → handled by ``requires_session``.
    * Verified email, no customer_id  → empty dashboard with /plans CTA.
    * Verified email + customer_id    → list of line cards.
    """
    identity: IdentityView | None = getattr(request.state, "identity", None)
    if identity is None or identity.customer_id is None:
        return templates.TemplateResponse(
            request,
            "dashboard_empty.html",
            {"email": getattr(identity, "email", None)},
        )

    customer_id = identity.customer_id
    clients = get_clients()

    # Pull subscriptions + the catalogue once. The catalogue lookup is
    # for human-readable plan names; one offering-list call beats N
    # per-subscription lookups for the dashboard's small N.
    subs: list[dict[str, Any]] = await clients.subscription.list_for_customer(
        customer_id
    )

    if not subs:
        return templates.TemplateResponse(
            request,
            "dashboard_empty.html",
            {"email": identity.email},
        )

    offerings: list[dict[str, Any]] = await clients.catalog.list_offerings()
    offering_name_by_id = {o["id"]: o.get("name", o["id"]) for o in offerings}

    # Per-subscription balance reads run in parallel — small N, but
    # the dashboard's sub-second budget is real.
    async def _safe_balance(sub_id: str) -> list[dict[str, Any]]:
        try:
            return await clients.subscription.get_balance(sub_id)
        except Exception:  # pragma: no cover - upstream error path
            # A 404 (no balances yet, e.g. pending_activation) renders
            # an empty bar set rather than failing the whole dashboard.
            return []

    balances_by_sub = await asyncio.gather(
        *(_safe_balance(s["id"]) for s in subs)
    )

    from bss_clock import now as clock_now

    now_iso = clock_now().isoformat()
    lines = [
        await _line_view(
            sub,
            offering_name=offering_name_by_id.get(
                sub.get("offeringId", ""), sub.get("offeringId", "")
            ),
            now_iso=now_iso,
            balances=bals,
        )
        for sub, bals in zip(subs, balances_by_sub)
    ]
    # Resolve pending offering names too, where present, so the
    # "switching to <name> on <date>" badge reads naturally.
    for line in lines:
        pending_id = line.get("pending_offering_id")
        if pending_id:
            line["pending_offering_name"] = offering_name_by_id.get(
                pending_id, pending_id
            )

    # v1.1 — targeted offers issued to this customer but not yet used. Shown
    # once at the customer level ("applies to your next order"), since an
    # issued offer is not bound to a specific line. Best-effort: a catalog/
    # loyalty hiccup just hides the block, never breaks the dashboard. The
    # portal holds no loyalty token — this goes through the catalog client.
    assigned_offers: list[dict[str, Any]] = []
    try:
        offers_resp = await clients.catalog.list_customer_offers(
            customer_id=customer_id, state="issued"
        )
        assigned_offers = [
            o for o in offers_resp.get("offers", []) if o.get("promotion")
        ]
    except Exception:  # noqa: BLE001 — entitlement display is non-critical
        log.warning("dashboard.assigned_offers.failed", exc_info=True)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "email": identity.email,
            "customer_id": customer_id,
            "lines": lines,
            "assigned_offers": assigned_offers,
        },
    )
