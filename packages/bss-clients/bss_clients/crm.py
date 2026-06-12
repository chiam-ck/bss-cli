"""CRMClient — service-to-service client for CRM (port 8002).

Covers TMF629 customer, TMF621 trouble ticket, TMF683 interaction,
plus the custom /crm-api/v1/ surface for case, KYC, and agent.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient

# v0.13.1 — friendly target-state → state-machine-trigger map. The
# CRM PATCH endpoint takes the trigger name, not the destination
# state; this lets callers (LLM tools especially) stay on the
# "to_state" mental model.
_STATE_TO_TRIGGER: dict[str, str] = {
    "in_progress": "take",
    "pending_customer": "await_customer",
    "resolved": "resolve",
    "closed": "close",
}

# v1.6 — ticket counterpart (services/crm/app/domain/ticket_state.py).
# ``in_progress`` is deliberately absent: three triggers land there
# (start/resume/reopen), so transition_ticket resolves it from the
# ticket's current state instead.
_TICKET_STATE_TO_TRIGGER: dict[str, str] = {
    "acknowledged": "ack",
    "pending": "wait",
    "resolved": "resolve",
    "closed": "close",
    "cancelled": "cancel",
}
_TICKET_IN_PROGRESS_TRIGGER_BY_SOURCE: dict[str, str] = {
    "acknowledged": "start",
    "pending": "resume",
    "resolved": "reopen",
}


class CRMClient(BSSClient):
    """Client for the CRM service (port 8002)."""

    def __init__(
        self,
        base_url: str = "http://crm:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    # ── Customer (TMF629) ────────────────────────────────────────────────

    async def create_customer(
        self,
        *,
        name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        """POST /tmf-api/customerManagement/v4/customer.

        ``name`` is split on the first whitespace into ``givenName`` +
        ``familyName`` (CRM requires both). At least one contact medium is
        required by CRM — default to a placeholder email if neither is given.
        """
        parts = name.strip().split(None, 1)
        given_name = parts[0] if parts else name
        family_name = parts[1] if len(parts) > 1 else given_name
        contact_mediums: list[dict[str, Any]] = []
        if email:
            contact_mediums.append(
                {"mediumType": "email", "value": email, "isPrimary": True}
            )
        if phone:
            contact_mediums.append(
                {"mediumType": "mobile", "value": phone, "isPrimary": not email}
            )
        if not contact_mediums:
            contact_mediums.append(
                {"mediumType": "email", "value": f"{given_name.lower()}@local", "isPrimary": True}
            )
        body: dict[str, Any] = {
            "givenName": given_name,
            "familyName": family_name,
            "contactMedium": contact_mediums,
        }
        resp = await self._request(
            "POST",
            "/tmf-api/customerManagement/v4/customer",
            json=body,
        )
        return resp.json()

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /tmf-api/customerManagement/v4/customer/{id}."""
        resp = await self._request(
            "GET", f"/tmf-api/customerManagement/v4/customer/{customer_id}"
        )
        return resp.json()

    async def find_customer_by_msisdn(self, msisdn: str) -> dict[str, Any]:
        """GET /tmf-api/customerManagement/v4/customer/by-msisdn/{msisdn} (v0.5+).

        Resolves MSISDN → subscription → customer in one hop. 404s when
        the number is unassigned or the owning customer was deleted.
        """
        resp = await self._request(
            "GET",
            f"/tmf-api/customerManagement/v4/customer/by-msisdn/{msisdn}",
        )
        return resp.json()

    async def find_customer_by_email(self, email: str) -> dict[str, Any]:
        """GET /tmf-api/customerManagement/v4/customer/by-email (v1.6.2).

        Exact match on a live email contact medium. Query param (not a
        path segment) so ``+`` addressing survives URL encoding. 404s
        when no customer carries the address.
        """
        resp = await self._request(
            "GET",
            "/tmf-api/customerManagement/v4/customer/by-email",
            params={"email": email},
        )
        return resp.json()

    async def list_customers(
        self,
        *,
        state: str | None = None,
        name_contains: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/customerManagement/v4/customer."""
        params: dict[str, Any] = {}
        if state:
            params["status"] = state
        if name_contains:
            params["name"] = name_contains
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        resp = await self._request(
            "GET",
            "/tmf-api/customerManagement/v4/customer",
            params=params,
        )
        return resp.json()

    async def update_customer(
        self, customer_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /tmf-api/customerManagement/v4/customer/{id}."""
        resp = await self._request(
            "PATCH",
            f"/tmf-api/customerManagement/v4/customer/{customer_id}",
            json=patch,
        )
        return resp.json()

    async def add_contact_medium(
        self,
        customer_id: str,
        *,
        medium_type: str,
        value: str,
    ) -> dict[str, Any]:
        """POST /tmf-api/customerManagement/v4/customer/{id}/contactMedium."""
        if medium_type == "email":
            characteristic = {"emailAddress": value}
        elif medium_type == "mobile":
            characteristic = {"phoneNumber": value}
        else:
            characteristic = {"value": value}
        resp = await self._request(
            "POST",
            f"/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium",
            json={"mediumType": medium_type, "characteristic": characteristic},
        )
        return resp.json()

    async def remove_contact_medium(
        self, customer_id: str, medium_id: str
    ) -> dict[str, Any]:
        """DELETE /tmf-api/customerManagement/v4/customer/{id}/contactMedium/{cm}."""
        resp = await self._request(
            "DELETE",
            f"/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium/{medium_id}",
        )
        return resp.json() if resp.content else {"id": medium_id, "removed": True}

    async def update_individual(
        self,
        customer_id: str,
        *,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /tmf-api/customerManagement/v4/customer/{id}/individual.

        v0.10 — partial update of the customer's display name. Used by
        the portal's /profile/contact name-update flow. At least one
        of ``given_name`` / ``family_name`` must be provided; the
        server raises ``policy.customer.individual.update.no_fields``
        otherwise.
        """
        body: dict[str, Any] = {}
        if given_name is not None:
            body["givenName"] = given_name
        if family_name is not None:
            body["familyName"] = family_name
        resp = await self._request(
            "PATCH",
            f"/tmf-api/customerManagement/v4/customer/{customer_id}/individual",
            json=body,
        )
        return resp.json()

    async def update_contact_medium(
        self, customer_id: str, medium_id: str, *, value: str
    ) -> dict[str, Any]:
        """PATCH /tmf-api/customerManagement/v4/customer/{id}/contactMedium/{cm}.

        v0.10 — phone + address updates from the self-serve portal go
        through this. Email updates go through the cross-schema
        ``bss_portal_auth.email_change`` flow; calling this with an
        email-typed medium is rejected server-side with
        ``policy.customer.contact_medium.email_must_use_change_flow``.
        """
        resp = await self._request(
            "PATCH",
            f"/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium/{medium_id}",
            json={"value": value},
        )
        return resp.json()

    async def list_contact_mediums(
        self, customer_id: str
    ) -> list[dict[str, Any]]:
        """Convenience: return the customer's active contact mediums.

        Wraps ``get_customer`` and pulls the ``contactMedium`` list off
        the TMF629 response. v0.10 — saves portal route handlers from
        re-implementing the extraction.
        """
        cust = await self.get_customer(customer_id)
        return list(cust.get("contactMedium") or [])

    async def close_customer(self, customer_id: str) -> dict[str, Any]:
        """PATCH customer with status=closed (policy-gated)."""
        return await self.update_customer(customer_id, {"status": "closed"})

    # ── KYC ──────────────────────────────────────────────────────────────

    async def attest_kyc(
        self,
        customer_id: str,
        *,
        provider: str,
        attestation_token: str,
        provider_reference: str | None = None,
        document_type: str = "nric",
        document_number: str | None = None,
        document_number_last4: str | None = None,
        document_number_hash: str | None = None,
        document_country: str = "SG",
        date_of_birth: str = "1990-01-01",
        nationality: str | None = "SG",
        verified_at: str | None = None,
        attestation_payload: dict[str, Any] | None = None,
        corroboration_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /crm-api/v1/customer/{id}/kyc-attestation.

        Full channel-layer attestation. Scenario runners can pass only
        ``provider`` + ``attestation_token`` and rely on the stub defaults
        for the rest; real channels fill in every field.

        ``document_number`` defaults to a deterministic per-customer stub
        (``"S" + customer_id-derived digits + "D"``) so portal-driven
        signups don't all hash to the same value and trip the
        ``customer.attest_kyc.document_hash_unique_per_tenant`` policy.
        Real channels override with the actual NRIC / passport number.
        """
        from datetime import datetime, timezone

        if document_number is None:
            # Stub: derive 7 digits from the hex tail of customer_id.
            # Pads with zeros if the id has <7 hex digits in its tail.
            tail = "".join(ch for ch in customer_id if ch.isdigit())
            digits = (tail + "0000000")[:7] if tail else "0000000"
            # If hex-only tail produced too few digits, fall back to
            # ord-summing the customer_id for distinctness.
            if digits == "0000000":
                digits = f"{abs(hash(customer_id)) % 10_000_000:07d}"
            document_number = f"S{digits}D"

        body: dict[str, Any] = {
            "provider": provider,
            "provider_reference": provider_reference or f"{provider}-{attestation_token[-8:]}",
            "document_type": document_type,
            "document_country": document_country,
            "date_of_birth": date_of_birth,
            "nationality": nationality,
            # bss-clients is a low-level transport package with no domain
            # dependencies — it cannot import bss-clock. Callers inside a
            # service that needs scenario-freezable time should pass
            # ``verified_at`` from ``bss_clock.now()``; this fallback is
            # only for ad-hoc CLI calls that don't care about scenario replay.
            "verified_at": verified_at or datetime.now(timezone.utc).isoformat(),  # noqa: bss-clock
            "attestation_payload": attestation_payload or {
                "token": attestation_token,
                "signature": f"stub-sig-{attestation_token[-16:]}",
            },
        }
        # v0.15 PII doctrine: pre-reduced last4 + hash form is preferred
        # (Didit path); raw document_number is the legacy stub path used by
        # prebaked + scenario callers. Both flow through the CRM API; the
        # service prefers reduced if both are supplied.
        if document_number is not None:
            body["document_number"] = document_number
        if document_number_last4 is not None:
            body["document_number_last4"] = document_number_last4
        if document_number_hash is not None:
            body["document_number_hash"] = document_number_hash
        if corroboration_id is not None:
            body["corroboration_id"] = corroboration_id
        resp = await self._request(
            "POST",
            f"/crm-api/v1/customer/{customer_id}/kyc-attestation",
            json=body,
        )
        return resp.json()

    async def get_kyc_status(self, customer_id: str) -> dict[str, Any]:
        """GET /crm-api/v1/customer/{id}/kyc-status."""
        resp = await self._request(
            "GET", f"/crm-api/v1/customer/{customer_id}/kyc-status"
        )
        return resp.json()

    # ── Case (/crm-api/v1/case) ──────────────────────────────────────────

    async def open_case(
        self,
        *,
        customer_id: str,
        subject: str,
        category: str,
        priority: str,
        description: str | None = None,
        opened_by_agent_id: str | None = None,
        chat_transcript_hash: str | None = None,
    ) -> dict[str, Any]:
        """POST /crm-api/v1/case.

        v0.12 added ``description``, ``opened_by_agent_id``, and
        ``chat_transcript_hash``. The first two were always supported
        server-side but not surfaced on the client; ``chat_transcript_hash``
        is new and links the case to a previously-stored transcript
        when the case is opened from the customer chat surface
        (``case.open_for_me``).
        """
        payload: dict[str, Any] = {
            "customer_id": customer_id,
            "subject": subject,
            "category": category,
            "priority": priority,
        }
        if description is not None:
            payload["description"] = description
        if opened_by_agent_id is not None:
            payload["opened_by_agent_id"] = opened_by_agent_id
        if chat_transcript_hash is not None:
            payload["chat_transcript_hash"] = chat_transcript_hash
        resp = await self._request(
            "POST",
            "/crm-api/v1/case",
            json=payload,
        )
        return resp.json()

    async def store_chat_transcript(
        self, *, hash_: str, customer_id: str, body: str
    ) -> dict[str, Any]:
        """POST /crm-api/v1/chat-transcript — v0.12.

        Idempotent on the hash PK. The orchestrator computes the
        SHA-256 and posts; the server re-computes and rejects
        mismatches so the column cannot be poisoned with a body that
        does not match its key.
        """
        resp = await self._request(
            "POST",
            "/crm-api/v1/chat-transcript",
            json={"hash": hash_, "customer_id": customer_id, "body": body},
        )
        return resp.json()

    async def get_chat_transcript(self, hash_: str) -> dict[str, Any]:
        """GET /crm-api/v1/chat-transcript/{hash} — v0.12. CSR-side only."""
        resp = await self._request(
            "GET", f"/crm-api/v1/chat-transcript/{hash_}"
        )
        return resp.json()

    async def get_case(self, case_id: str) -> dict[str, Any]:
        """GET /crm-api/v1/case/{id}."""
        resp = await self._request("GET", f"/crm-api/v1/case/{case_id}")
        return resp.json()

    async def list_cases(
        self,
        *,
        customer_id: str | None = None,
        state: str | None = None,
        agent_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /crm-api/v1/case.

        v1.6 — the agent filter now sends ``assignedAgentId`` (the query
        param the CRM service actually reads; ``agentId`` was silently
        ignored), and ``limit``/``offset`` page the cockpit case queue.
        """
        params: dict[str, Any] = {}
        if customer_id:
            params["customerId"] = customer_id
        if state:
            params["state"] = state
        if agent_id:
            params["assignedAgentId"] = agent_id
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        resp = await self._request("GET", "/crm-api/v1/case", params=params)
        return resp.json()

    async def add_case_note(self, case_id: str, *, body: str) -> dict[str, Any]:
        """POST /crm-api/v1/case/{id}/note."""
        resp = await self._request(
            "POST",
            f"/crm-api/v1/case/{case_id}/note",
            json={"body": body},
        )
        return resp.json()

    async def update_case(
        self, case_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /crm-api/v1/case/{id}."""
        resp = await self._request(
            "PATCH", f"/crm-api/v1/case/{case_id}", json=patch
        )
        return resp.json()

    async def transition_case(
        self, case_id: str, *, to_state: str | None = None, trigger: str | None = None
    ) -> dict[str, Any]:
        """PATCH /crm-api/v1/case/{id} with a state transition.

        v0.13.1 — the API takes ``{"trigger": ...}`` (state-machine
        trigger name), NOT ``{"state": ...}`` (target state). We
        convert here so callers can stay on the friendlier "to_state"
        shape; unknown target states raise ValueError early so the
        LLM gets a structured error rather than a server 422.

        v1.6 — ``trigger`` may be passed directly for transitions the
        to_state map can't express (``resume``: pending_customer →
        in_progress collides with ``take``: open → in_progress).
        """
        if trigger is None:
            if to_state is None:
                raise ValueError("transition_case needs to_state or trigger")
            trigger = _STATE_TO_TRIGGER.get(to_state)
            if trigger is None:
                raise ValueError(
                    f"Unknown target state {to_state!r}; valid targets: "
                    f"{sorted(_STATE_TO_TRIGGER)}"
                )
        resp = await self._request(
            "PATCH",
            f"/crm-api/v1/case/{case_id}",
            json={"trigger": trigger},
        )
        return resp.json()

    async def update_case_priority(
        self, case_id: str, *, priority: str
    ) -> dict[str, Any]:
        """PATCH case priority."""
        return await self.update_case(case_id, {"priority": priority})

    async def close_case(
        self, case_id: str, *, resolution_code: str
    ) -> dict[str, Any]:
        """POST /crm-api/v1/case/{id}/close — policy-gated.

        v0.13.1 — body uses snake_case ``resolution_code`` to match the
        ``CloseCaseRequest`` Pydantic schema in
        ``services/crm/app/schemas/internal/case.py``. The prior
        ``resolutionCode`` (camelCase) caused 422s on every close.
        """
        resp = await self._request(
            "POST",
            f"/crm-api/v1/case/{case_id}/close",
            json={"resolution_code": resolution_code},
        )
        return resp.json()

    # ── Port request (v0.17 MNP, operator-only) ─────────────────────────

    async def list_port_requests(
        self,
        *,
        state: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """GET /crm-api/v1/port-requests."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if state:
            params["state"] = state
        if direction:
            params["direction"] = direction
        resp = await self._request(
            "GET", "/crm-api/v1/port-requests", params=params
        )
        return resp.json()

    async def get_port_request(self, port_id: str) -> dict[str, Any]:
        """GET /crm-api/v1/port-requests/{id}."""
        resp = await self._request(
            "GET", f"/crm-api/v1/port-requests/{port_id}"
        )
        return resp.json()

    async def create_port_request(
        self,
        *,
        direction: str,
        donor_carrier: str,
        donor_msisdn: str,
        requested_port_date: str,
        target_subscription_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /crm-api/v1/port-requests."""
        body = {
            "direction": direction,
            "donorCarrier": donor_carrier,
            "donorMsisdn": donor_msisdn,
            "requestedPortDate": requested_port_date,
        }
        if target_subscription_id is not None:
            body["targetSubscriptionId"] = target_subscription_id
        resp = await self._request(
            "POST", "/crm-api/v1/port-requests", json=body
        )
        return resp.json()

    async def approve_port_request(self, port_id: str) -> dict[str, Any]:
        """POST /crm-api/v1/port-requests/{id}/approve."""
        resp = await self._request(
            "POST", f"/crm-api/v1/port-requests/{port_id}/approve"
        )
        return resp.json()

    async def reject_port_request(
        self, port_id: str, *, reason: str
    ) -> dict[str, Any]:
        """POST /crm-api/v1/port-requests/{id}/reject."""
        resp = await self._request(
            "POST",
            f"/crm-api/v1/port-requests/{port_id}/reject",
            json={"reason": reason},
        )
        return resp.json()

    # ── Ticket (TMF621) ──────────────────────────────────────────────────

    async def open_ticket(
        self,
        *,
        ticket_type: str,
        subject: str,
        case_id: str | None = None,
        customer_id: str | None = None,
        order_id: str | None = None,
        subscription_id: str | None = None,
        service_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /tmf-api/troubleTicket/v4/troubleTicket.

        v1.6 — ``customerId``/``caseId`` are sent as direct fields; the
        CRM ``CreateTicketRequest`` schema reads those, and the previous
        relatedEntity-only payload failed validation (`customerId`
        required) on every call that carried a customer. relatedEntity
        is still attached for order/subscription/service references
        (TMF-shaped, tolerated by the service today, parsed when the
        schema grows them).
        """
        body: dict[str, Any] = {
            "ticketType": ticket_type,
            "subject": subject,
        }
        if customer_id:
            body["customerId"] = customer_id
        if case_id:
            body["caseId"] = case_id
        relates = []
        if order_id:
            relates.append({"entityType": "order", "id": order_id})
        if subscription_id:
            relates.append({"entityType": "subscription", "id": subscription_id})
        if service_id:
            relates.append({"entityType": "service", "id": service_id})
        if relates:
            body["relatedEntity"] = relates
        resp = await self._request(
            "POST", "/tmf-api/troubleTicket/v4/troubleTicket", json=body
        )
        return resp.json()

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """GET /tmf-api/troubleTicket/v4/troubleTicket/{id}."""
        resp = await self._request(
            "GET", f"/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}"
        )
        return resp.json()

    async def list_tickets(
        self,
        *,
        customer_id: str | None = None,
        case_id: str | None = None,
        state: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/troubleTicket/v4/troubleTicket."""
        params: dict[str, Any] = {}
        if customer_id:
            params["customerId"] = customer_id
        if case_id:
            params["caseId"] = case_id
        if state:
            params["state"] = state
        if agent_id:
            params["agentId"] = agent_id
        resp = await self._request(
            "GET", "/tmf-api/troubleTicket/v4/troubleTicket", params=params
        )
        return resp.json()

    async def assign_ticket(
        self, ticket_id: str, *, agent_id: str
    ) -> dict[str, Any]:
        """PATCH troubleTicket with assignedToAgentId.

        v1.6 — the field is ``assignedToAgentId`` (UpdateTicketRequest);
        the previous ``assignedAgent`` key was silently ignored by the
        service, making every assign a no-op.
        """
        resp = await self._request(
            "PATCH",
            f"/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}",
            json={"assignedToAgentId": agent_id},
        )
        return resp.json()

    async def transition_ticket(
        self, ticket_id: str, *, to_state: str | None = None, trigger: str | None = None
    ) -> dict[str, Any]:
        """POST /tmf-api/troubleTicket/v4/troubleTicket/{id}/transition.

        v1.6 — the API takes ``{"trigger": ...}``; the previous
        ``{"toState": ...}`` body failed validation on every call. We
        map target state → trigger here (mirroring transition_case).
        ``in_progress`` is reachable via three triggers (start / resume
        / reopen), so that target costs one ``get_ticket`` read to pick
        the right one; reads are free. ``trigger=`` overrides directly.
        """
        if trigger is None:
            if to_state is None:
                raise ValueError("transition_ticket needs to_state or trigger")
            if to_state == "in_progress":
                current = await self.get_ticket(ticket_id)
                src = str(current.get("state", ""))
                trigger = _TICKET_IN_PROGRESS_TRIGGER_BY_SOURCE.get(src)
                if trigger is None:
                    raise ValueError(
                        f"No transition to in_progress from {src!r}; valid "
                        f"sources: {sorted(_TICKET_IN_PROGRESS_TRIGGER_BY_SOURCE)}"
                    )
            else:
                trigger = _TICKET_STATE_TO_TRIGGER.get(to_state)
                if trigger is None:
                    raise ValueError(
                        f"Unknown target state {to_state!r}; valid targets: "
                        f"{sorted(_TICKET_STATE_TO_TRIGGER)} + ['in_progress']"
                    )
        resp = await self._request(
            "POST",
            f"/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}/transition",
            json={"trigger": trigger},
        )
        return resp.json()

    async def resolve_ticket(
        self, ticket_id: str, *, resolution_notes: str
    ) -> dict[str, Any]:
        """POST /tmf-api/troubleTicket/v4/troubleTicket/{id}/resolve."""
        resp = await self._request(
            "POST",
            f"/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}/resolve",
            json={"resolutionNotes": resolution_notes},
        )
        return resp.json()

    async def close_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Transition ticket → closed."""
        return await self.transition_ticket(ticket_id, to_state="closed")

    async def cancel_ticket(self, ticket_id: str) -> dict[str, Any]:
        """POST /tmf-api/troubleTicket/v4/troubleTicket/{id}/cancel — destructive."""
        resp = await self._request(
            "POST", f"/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}/cancel"
        )
        return resp.json()

    # ── Interaction (TMF683) ─────────────────────────────────────────────

    async def log_interaction(
        self,
        *,
        customer_id: str,
        summary: str,
        channel: str | None = None,
        direction: str = "inbound",
        body_text: str | None = None,
    ) -> dict[str, Any]:
        """POST /tmf-api/customerInteractionManagement/v1/interaction.

        Server schema is TMF683-shaped: ``summary`` is required, ``channel``
        defaults to the caller's ``X-BSS-Channel`` context, ``direction``
        defaults to ``inbound``. ``body_text`` (if present) is sent as the
        optional ``body`` free-text field.
        """
        payload: dict[str, Any] = {
            "customerId": customer_id,
            "summary": summary,
            "direction": direction,
        }
        if channel is not None:
            payload["channel"] = channel
        if body_text is not None:
            payload["body"] = body_text
        resp = await self._request(
            "POST",
            "/tmf-api/customerInteractionManagement/v1/interaction",
            json=payload,
        )
        return resp.json()

    async def list_interactions(
        self, customer_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/customerInteractionManagement/v1/interaction."""
        resp = await self._request(
            "GET",
            "/tmf-api/customerInteractionManagement/v1/interaction",
            params={"customerId": customer_id, "limit": limit},
        )
        return resp.json()

    # ── Agent ────────────────────────────────────────────────────────────

    async def list_agents(
        self, *, state: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /crm-api/v1/agent."""
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        resp = await self._request("GET", "/crm-api/v1/agent", params=params)
        return resp.json()

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        """GET /crm-api/v1/agent/{id}."""
        resp = await self._request("GET", f"/crm-api/v1/agent/{agent_id}")
        return resp.json()
