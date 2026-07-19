"""``/esim/<subscription_id>`` — read-only LPA activation code + QR (v0.10 PR 6).

V0_10_0.md Track 5. URL note: the phase doc names this ``/esim/<service_id>``,
but in v0.10 we key the route on ``subscription_id`` because the eSIM is
1:1 with the subscription (one LPA per line) and ``subscription.get`` is
the existing ownership read. Switching the URL key to ``service_id``
would require a new service-inventory client method that the audit
flagged as missing — out of scope for PR 6.

**Production fidelity caveat (DECISIONS 2026-04-27):**

This route is a deliberately simplified read-only re-display of the
LPA activation code minted at signup. It is NOT a production-grade
eSIM redownload. Real GSMA SGP.22 redownload requires the operator
to call SM-DP+ to release / re-arm a profile (or mint a fresh
activation code bound to a new ICCID) before a new device can
install it. v0.10's SM-DP+ is simulated (provisioning-sim), so
there's nothing to re-arm — and the simplification is honest:
showing the same code on every visit, no state change.

The template caption tells the customer that if the code isn't
working on a new device they should contact support; the
operator-side rearm work ships post-v0.1 as ``ESIM_PROFILE_REARM``
(see ROADMAP.md non-goals + the v0.10 DECISIONS entry above).

Doctrine:

* Read-only — no step-up, no portal_action audit row on the regular
  path (non-sensitive read). The audit infra is reserved for the
  ``?show_full=1`` debug branch, which is admin-only and lands when
  the CSR portal post-login features ship (v0.12). For v0.10, the
  ``?show_full=1`` query param is silently ignored — the customer
  surface always shows last-4 ICCID / IMSI.
* ``customer_id`` from ``request.state.customer_id``; ``subscription_id``
  from the URL is checked for ownership against ``customerId`` on
  ``subscription.get``. Cross-customer attempts return 403, not 404,
  so the response distinguishes "not yours" from "doesn't exist"
  only via the resource ID being valid in the first place — same
  forensic posture as PR 4 / PR 5.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from ..clients import get_clients
from ..error_messages import render
from ..qrpng import activation_qr_data_uri
from ..security import requires_linked_customer
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


_OWNERSHIP_RULE = "policy.ownership.subscription_not_owned"


def _last4(value: str | None) -> str:
    if not value:
        return "----"
    return f"…{value[-4:]}" if len(value) > 4 else value


@router.get("/esim/{subscription_id}", response_class=HTMLResponse)
async def esim_view(
    request: Request,
    subscription_id: str,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Render the customer's LPA activation code + an inline PNG QR.

    Server-side ownership: load the subscription, verify
    ``customerId == request.state.customer_id``. On mismatch (or
    not-found) → 403 with a customer-facing message; the deliberate
    forensic posture is to not distinguish "exists but not yours"
    from "does not exist" through the response status.
    """
    clients = get_clients()

    try:
        sub: dict[str, Any] = await clients.subscription.get(subscription_id)
    except Exception:
        return templates.TemplateResponse(
            request,
            "esim_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    if sub.get("customerId") != customer_id:
        return templates.TemplateResponse(
            request,
            "esim_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    iccid = sub.get("iccid")
    activation_code: str | None = None
    smdp_server: str | None = None
    matching_id: str | None = None
    if iccid:
        try:
            activation = await clients.inventory.get_activation_code(iccid)
            activation_code = activation.get("activation_code") or activation.get(
                "activationCode"
            )
            smdp_server = activation.get("smdp_server") or activation.get("smdpServer")
            matching_id = activation.get("matching_id") or activation.get("matchingId")
        except Exception:  # pragma: no cover - upstream lookup failure
            log.warning(
                "portal.esim.activation_lookup_failed",
                subscription_id=subscription_id,
                iccid=iccid,
            )

    qr_data_uri = activation_qr_data_uri(activation_code) if activation_code else None

    return templates.TemplateResponse(
        request,
        "esim.html",
        {
            "subscription": sub,
            "activation_code": activation_code,
            "smdp_server": smdp_server,
            "matching_id": matching_id,
            "qr_data_uri": qr_data_uri,
            "iccid_last4": _last4(iccid),
            "imsi_last4": _last4(sub.get("imsi")),
            "msisdn": sub.get("msisdn"),
            "state": sub.get("state"),
        },
    )
