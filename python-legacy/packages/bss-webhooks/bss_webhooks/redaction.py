"""Per-provider payload redaction before logging or persistence.

Every provider's response shape leaks something we don't want in
``integrations.external_call.redacted_payload`` or in structlog records:

* Resend responses surface the recipient ``to`` address.
* Stripe responses surface customer email and (post-tokenization)
  card last4 — last4 is allowed; full PAN must never appear, but
  Stripe never returns it post-tokenization, so this is defense-in-depth.
* Didit responses surface raw document numbers and dates of birth —
  these MUST be hashed inside ``DiditKycAdapter.fetch_attestation``
  before they cross the BSS boundary; this redactor is the second
  line of defense against accidental persistence.

The redactor is provider-keyed. Unknown providers fall through to a
permissive identity transform — the doctrine (greppable) is that
``redact_provider_payload`` is called at every persistence point,
even if the provider's redactor is a no-op today.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

# Mask used for obviously-secret fields (signatures, tokens). Hashes
# (e.g. of document numbers) are SHA-256 hex-truncated to 16 chars
# so they're greppable across rows but not reversible.
_MASK = "[redacted]"


def _hash_pii(value: str) -> str:
    """Stable, greppable, non-reversible hash for PII strings."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def redact_provider_payload(
    *, provider: str, body: Any
) -> Any:
    """Return a redacted copy of ``body`` keyed on ``provider``.

    Pure (no IO). Recursive across dicts and lists. Strings, ints,
    bools, and None pass through unchanged unless the field name
    matches a per-provider rule.

    Unknown providers return a deep copy unchanged. New providers
    must add an entry in ``_REDACTORS`` rather than relying on the
    fallback.
    """
    redactor = _REDACTORS.get(provider, _identity)
    return redactor(_deep_copy(body))


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


def _identity(value: Any) -> Any:
    return value


# ── per-provider redactors ─────────────────────────────────────────


def _redact_resend(value: Any) -> Any:
    """Mask recipient + sender to keep customer email out of forensics."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = k.lower()
            if lk in {"to", "from", "reply_to", "cc", "bcc"}:
                out[k] = _MASK
            else:
                out[k] = _redact_resend(v)
        return out
    if isinstance(value, list):
        return [_redact_resend(item) for item in value]
    return value


def _redact_stripe(value: Any) -> Any:
    """Mask customer email + billing PII; keep last4 + decline_code (ops needs them)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = k.lower()
            if lk in {"email", "name", "phone", "address", "billing_details"}:
                out[k] = _MASK
            elif lk in {"number", "cvc", "cvv"}:
                # Card details should never appear post-tokenization;
                # mask defensively if they do.
                out[k] = _MASK
            else:
                out[k] = _redact_stripe(v)
        return out
    if isinstance(value, list):
        return [_redact_stripe(item) for item in value]
    return value


_DOC_NUMBER_FIELDS = {
    "document_number",
    "id_number",
    "national_id",
    "nric",
    "passport_number",
}


def _redact_didit(value: Any) -> Any:
    """Hash raw document numbers + DOB; mask names. Hashes stay queryable."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = k.lower()
            if lk in _DOC_NUMBER_FIELDS and isinstance(v, str):
                out[k] = _hash_pii(v)
            elif lk in {"date_of_birth", "dob", "birth_date"} and isinstance(v, str):
                out[k] = _hash_pii(v)
            elif lk in {"first_name", "last_name", "full_name", "name"}:
                out[k] = _MASK
            else:
                out[k] = _redact_didit(v)
        return out
    if isinstance(value, list):
        return [_redact_didit(item) for item in value]
    return value


_REDACTORS: dict[str, Callable[[Any], Any]] = {
    "resend": _redact_resend,
    "stripe": _redact_stripe,
    "didit": _redact_didit,
}
