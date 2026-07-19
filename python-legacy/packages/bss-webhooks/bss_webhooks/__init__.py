"""bss-webhooks — shared webhook substrate for BSS-CLI provider integrations.

v0.14 ships the cross-cutting concerns that every provider webhook
receiver needs:

* :mod:`bss_webhooks.signatures` — HMAC verification across the three
  schemes BSS-CLI consumes: ``svix`` (Resend, v0.14), ``stripe`` (v0.16),
  ``didit_hmac`` (v0.15). All three are built upfront so v0.16 isn't
  touching shared crypto under payment-scope pressure.
* :mod:`bss_webhooks.store` — :class:`WebhookEventStore` (idempotent
  persist on ``(provider, event_id)``) + :class:`ExternalCallStore`
  (forensic per-call log).
* :mod:`bss_webhooks.idempotency` — deterministic
  :func:`idempotency_key` for retry-safe outbound provider calls.
* :mod:`bss_webhooks.redaction` — per-provider payload redaction
  before persisting (Stripe leaks customer email, Didit leaks
  document numbers — never log raw).

Provider-specific adapters (``ResendEmailAdapter`` etc.) live in their
domain packages and *use* this substrate; this package does not
contain provider-specific business logic.
"""

from .idempotency import idempotency_key
from .redaction import redact_provider_payload
from .signatures import (
    SignatureScheme,
    WebhookSignatureError,
    verify_signature,
)
from .store import ExternalCallStore, WebhookEventStore

__all__ = [
    "ExternalCallStore",
    "SignatureScheme",
    "WebhookEventStore",
    "WebhookSignatureError",
    "idempotency_key",
    "redact_provider_payload",
    "verify_signature",
]
