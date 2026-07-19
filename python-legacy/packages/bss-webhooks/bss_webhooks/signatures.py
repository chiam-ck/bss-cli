"""HMAC signature verification for inbound provider webhooks.

Three schemes, all HMAC-SHA-256, differing in header format + canonical
signed payload:

* ``svix`` — Resend (v0.14). Headers: ``svix-id``, ``svix-timestamp``,
  ``svix-signature``. Signed payload: ``f"{id}.{timestamp}.{body}"``.
  The ``svix-signature`` header carries one or more space-separated
  ``v1,<base64>`` entries (key rotation support); any matching entry
  validates.

* ``stripe`` — Stripe (v0.16). Header: ``Stripe-Signature``,
  comma-delimited fields including ``t=<timestamp>`` and one or more
  ``v1=<hex>`` entries. Signed payload: ``f"{timestamp}.{body}"``.

* ``didit_hmac`` — Didit (v0.15). Header: ``X-Signature-V2``, value
  ``f"{timestamp}.{hex}"``. Signed payload: ``f"{timestamp}.{body}"``.

All three validate timestamp freshness against ``max_skew_seconds``
(default 300s) and compare with :func:`hmac.compare_digest`. Any
mismatch — bad signature, stale timestamp, malformed header — raises
:class:`WebhookSignatureError` with a stable error code suitable for
ops triage.

The function is built v0.14-complete (all three schemes) even though
only ``svix`` has a v0.14 consumer. This is deliberate: v0.16 must not
be the first time anyone touches shared HMAC code under payment-scope
pressure.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import Final, Literal, Mapping

SignatureScheme = Literal["svix", "stripe", "didit_hmac"]

_DEFAULT_MAX_SKEW_SECONDS: Final[int] = 300


class WebhookSignatureError(ValueError):
    """Raised on any signature-verification failure.

    The ``code`` attribute carries one of:

    * ``"missing_header"`` — required header(s) not present.
    * ``"malformed_header"`` — header parseable but does not match the
      provider's documented format (missing ``v1=``, no timestamp,
      truncated signature).
    * ``"replay_window"`` — timestamp older than ``max_skew_seconds``
      (or in the future by the same margin).
    * ``"signature_mismatch"`` — HMAC compare returned no match.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def verify_signature(
    *,
    secret: str | bytes,
    body: bytes,
    headers: Mapping[str, str],
    scheme: SignatureScheme,
    max_skew_seconds: int = _DEFAULT_MAX_SKEW_SECONDS,
    now: float | None = None,
) -> None:
    """Verify the signature on an inbound webhook request.

    :param secret: Provider-shared signing secret. The svix scheme
        accepts the dashboard-displayed ``whsec_<base64>`` form and
        decodes it; the other schemes accept either str or bytes.
    :param body: Raw request body bytes — *not* re-serialized JSON.
        Re-serialization changes whitespace and breaks the HMAC.
    :param headers: Request headers, case-insensitive lookup. Pass the
        request's headers dict directly; we lowercase keys internally.
    :param scheme: One of the three documented schemes.
    :param max_skew_seconds: Permissible clock skew on either side.
    :param now: Test seam for the current unix timestamp.

    Raises :class:`WebhookSignatureError` on any failure. Returns
    ``None`` on success.
    """
    headers_lower = {k.lower(): v for k, v in headers.items()}

    if scheme == "svix":
        _verify_svix(secret, body, headers_lower, max_skew_seconds, now)
    elif scheme == "stripe":
        _verify_stripe(secret, body, headers_lower, max_skew_seconds, now)
    elif scheme == "didit_hmac":
        _verify_didit_hmac(secret, body, headers_lower, max_skew_seconds, now)
    else:  # pragma: no cover — Literal type ensures unreachable.
        raise WebhookSignatureError(
            "malformed_header", f"unknown scheme: {scheme!r}"
        )


# ── svix (Resend) ───────────────────────────────────────────────────


def _verify_svix(
    secret: str | bytes,
    body: bytes,
    headers: Mapping[str, str],
    max_skew_seconds: int,
    now: float | None,
) -> None:
    msg_id = headers.get("svix-id")
    timestamp = headers.get("svix-timestamp")
    signature_header = headers.get("svix-signature")

    if not (msg_id and timestamp and signature_header):
        raise WebhookSignatureError(
            "missing_header",
            "svix-id, svix-timestamp, and svix-signature headers required",
        )

    _check_timestamp(timestamp, max_skew_seconds, now)

    # secret format: ``whsec_<base64>``. Base64-decode to get the raw key.
    key = _decode_svix_secret(secret)
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected_sig = hmac.new(key, signed, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected_sig).decode()

    # Header format: space-separated entries like ``v1,<base64> v1,<base64>``.
    # Any matching ``v1`` entry validates. Iterate all to keep timing
    # independent of which-if-any matched.
    matched = False
    for entry in signature_header.split():
        if not entry.startswith("v1,"):
            continue
        candidate = entry[len("v1,"):]
        if hmac.compare_digest(candidate, expected_b64):
            matched = True

    if not matched:
        raise WebhookSignatureError(
            "signature_mismatch", "no v1 signature entry matched"
        )


def _decode_svix_secret(secret: str | bytes) -> bytes:
    """Accept ``whsec_<base64>`` form and return raw key bytes.

    Resend (Svix-backed) shows secrets in dashboard prefixed with
    ``whsec_``; the part after the prefix is the base64-encoded key.
    Some test setups pass the raw bytes directly; we accept both.
    """
    if isinstance(secret, bytes):
        return secret
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[len("whsec_"):])
        except (binascii.Error, ValueError) as exc:
            raise WebhookSignatureError(
                "malformed_header",
                f"svix secret after 'whsec_' is not valid base64: {exc}",
            ) from exc
    return secret.encode()


# ── stripe ──────────────────────────────────────────────────────────


def _verify_stripe(
    secret: str | bytes,
    body: bytes,
    headers: Mapping[str, str],
    max_skew_seconds: int,
    now: float | None,
) -> None:
    sig_header = headers.get("stripe-signature")
    if not sig_header:
        raise WebhookSignatureError(
            "missing_header", "Stripe-Signature header required"
        )

    timestamp: str | None = None
    candidates: list[str] = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        v = v.strip()
        if k == "t":
            timestamp = v
        elif k == "v1":
            candidates.append(v)

    if timestamp is None:
        raise WebhookSignatureError(
            "malformed_header", "Stripe-Signature missing 't=' field"
        )
    if not candidates:
        raise WebhookSignatureError(
            "malformed_header", "Stripe-Signature missing v1 entries"
        )

    _check_timestamp(timestamp, max_skew_seconds, now)

    key = secret.encode() if isinstance(secret, str) else secret
    signed = f"{timestamp}.".encode() + body
    expected_hex = hmac.new(key, signed, hashlib.sha256).hexdigest()

    matched = False
    for cand in candidates:
        if hmac.compare_digest(cand, expected_hex):
            matched = True

    if not matched:
        raise WebhookSignatureError(
            "signature_mismatch", "no v1 signature matched"
        )


# ── didit_hmac ──────────────────────────────────────────────────────


def _verify_didit_hmac(
    secret: str | bytes,
    body: bytes,
    headers: Mapping[str, str],
    max_skew_seconds: int,
    now: float | None,
) -> None:
    """Verify Didit Webhooks v3.0 signature.

    Headers as observed in real sandbox deliveries (2026-05-02):

        X-Signature-V2:    <hex>   — HMAC-SHA-256(secret, body)  [body-only]
        X-Signature:       <hex>   — alias of V2
        X-Signature-Simple: <hex>  — different hex; algorithm undocumented
        X-Timestamp:       <unix epoch seconds>

    Despite its name, ``X-Signature-V2`` is computed over the body
    alone — the timestamp is NOT mixed into the signature input
    (verified against three real deliveries by computing the formula
    offline). Reproduction:

        hmac_sha256(secret, body).hexdigest() == X-Signature-V2

    The ``X-Timestamp`` header is checked separately for the replay
    freshness window. Because the timestamp doesn't bind into the HMAC
    input, replay protection is weaker than Stripe-style: a captured
    webhook could be re-delivered with a refreshed timestamp. The
    corroboration row's ``decision_body_digest`` record + the unique
    constraint on ``(provider, provider_session_id)`` catch the same-
    session replay at the DB level.
    """
    sig_hex = headers.get("x-signature-v2") or headers.get("x-signature")
    if not sig_hex:
        raise WebhookSignatureError(
            "missing_header",
            "X-Signature-V2 (or X-Signature) header required",
        )

    timestamp = headers.get("x-timestamp")
    if not timestamp:
        raise WebhookSignatureError(
            "missing_header", "X-Timestamp header required"
        )

    if not all(c in "0123456789abcdefABCDEF" for c in sig_hex):
        raise WebhookSignatureError(
            "malformed_header", "X-Signature-V2 must be hex"
        )

    _check_timestamp(timestamp, max_skew_seconds, now)

    key = secret.encode() if isinstance(secret, str) else secret
    expected_hex = hmac.new(key, body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(sig_hex.lower(), expected_hex):
        raise WebhookSignatureError(
            "signature_mismatch", "didit_hmac signature did not match"
        )


# ── shared helpers ──────────────────────────────────────────────────


def _check_timestamp(
    timestamp: str, max_skew_seconds: int, now: float | None
) -> None:
    """Reject replay-window violations.

    Timestamp may be unix-seconds (Stripe, Didit) or unix-millis
    (Svix). We accept both: any value > 1e12 is treated as millis.
    """
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookSignatureError(
            "malformed_header", f"timestamp not an integer: {timestamp!r}"
        ) from exc

    if ts > 1_000_000_000_000:  # > year 33658 in seconds → must be millis
        ts_seconds = ts / 1000.0
    else:
        ts_seconds = float(ts)

    current = time.time() if now is None else now
    skew = abs(current - ts_seconds)
    if skew > max_skew_seconds:
        raise WebhookSignatureError(
            "replay_window",
            f"timestamp skew {skew:.1f}s exceeds {max_skew_seconds}s",
        )
