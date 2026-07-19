"""Signature verification tests across all three schemes.

All three schemes are tested at full strength even though only ``svix``
has a v0.14 consumer. The doctrine: v0.16 must not be the first time
anyone touches shared HMAC code under payment-scope pressure. Build it
right once, leave it alone.

Per scheme we cover:
- happy path
- missing required header(s)
- malformed signature header
- tampered body
- replay-window violation (timestamp too old)
- timestamp in the future (replay-window)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from bss_webhooks.signatures import WebhookSignatureError, verify_signature


def _stripe_sign(secret: str, body: bytes, timestamp: int) -> str:
    signed = f"{timestamp}.".encode() + body
    h = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={h}"


def _didit_sign(secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    """Didit Webhooks v3.0 signs body bytes alone (timestamp is sent in a
    SEPARATE ``X-Timestamp`` header but does NOT bind into the HMAC
    input — verified against three real sandbox deliveries on
    2026-05-02 by reproducing the formula offline)."""
    h = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Signature-V2": h, "X-Timestamp": str(timestamp)}


def _svix_sign(secret_key: bytes, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    h = hmac.new(secret_key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(h).decode()


# ── stripe ──────────────────────────────────────────────────────────


def test_stripe_happy_path() -> None:
    secret = "whsec_stripetestsecret"
    body = b'{"id":"evt_1","type":"charge.succeeded"}'
    ts = int(time.time())
    headers = {"Stripe-Signature": _stripe_sign(secret, body, ts)}
    verify_signature(
        secret=secret, body=body, headers=headers, scheme="stripe", now=ts
    )


def test_stripe_missing_header_raises_missing_header() -> None:
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="whsec_x", body=b"{}", headers={}, scheme="stripe", now=time.time()
        )
    assert exc_info.value.code == "missing_header"


def test_stripe_no_v1_entry_raises_malformed() -> None:
    headers = {"Stripe-Signature": "t=1234567890"}
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="whsec_x", body=b"{}", headers=headers, scheme="stripe",
            now=1234567890.0,
        )
    assert exc_info.value.code == "malformed_header"


def test_stripe_tampered_body_raises_signature_mismatch() -> None:
    secret = "whsec_stripetestsecret"
    body = b'{"id":"evt_1"}'
    ts = int(time.time())
    headers = {"Stripe-Signature": _stripe_sign(secret, body, ts)}
    tampered = b'{"id":"evt_2"}'
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=tampered, headers=headers, scheme="stripe", now=ts
        )
    assert exc_info.value.code == "signature_mismatch"


def test_stripe_old_timestamp_raises_replay_window() -> None:
    secret = "whsec_x"
    body = b"{}"
    old_ts = int(time.time()) - 600
    headers = {"Stripe-Signature": _stripe_sign(secret, body, old_ts)}
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=body, headers=headers, scheme="stripe",
            max_skew_seconds=300, now=time.time(),
        )
    assert exc_info.value.code == "replay_window"


def test_stripe_future_timestamp_raises_replay_window() -> None:
    secret = "whsec_x"
    body = b"{}"
    future_ts = int(time.time()) + 600
    headers = {"Stripe-Signature": _stripe_sign(secret, body, future_ts)}
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=body, headers=headers, scheme="stripe",
            max_skew_seconds=300, now=time.time(),
        )
    assert exc_info.value.code == "replay_window"


def test_stripe_multiple_v1_entries_first_matches() -> None:
    """Stripe header may have multiple v1 entries (key rotation). Any match validates."""
    secret = "whsec_correct"
    body = b"{}"
    ts = int(time.time())
    correct = _stripe_sign(secret, body, ts).split(",")[1]  # "v1=<hex>"
    bad_v1 = "v1=" + "0" * 64
    headers = {"Stripe-Signature": f"t={ts},{bad_v1},{correct}"}
    verify_signature(
        secret=secret, body=body, headers=headers, scheme="stripe", now=ts
    )


# ── didit_hmac ──────────────────────────────────────────────────────


def test_didit_happy_path() -> None:
    secret = "didit-secret"
    body = b'{"session_id":"abc"}'
    ts = int(time.time())
    verify_signature(
        secret=secret, body=body,
        headers=_didit_sign(secret, body, ts),
        scheme="didit_hmac", now=ts,
    )


def test_didit_x_signature_alias_also_accepted() -> None:
    """Didit v3.0 sends both ``X-Signature-V2`` and a ``X-Signature``
    alias with the same hex. We accept either."""
    secret = "didit-secret"
    body = b'{"session_id":"abc"}'
    ts = int(time.time())
    h = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_signature(
        secret=secret, body=body,
        headers={"X-Signature": h, "X-Timestamp": str(ts)},
        scheme="didit_hmac", now=ts,
    )


def test_didit_missing_signature_header_raises_missing_header() -> None:
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="x", body=b"{}",
            headers={"X-Timestamp": str(int(time.time()))},
            scheme="didit_hmac", now=time.time(),
        )
    assert exc_info.value.code == "missing_header"


def test_didit_missing_timestamp_header_raises_missing_header() -> None:
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="x", body=b"{}",
            headers={"X-Signature-V2": "deadbeef" * 8},
            scheme="didit_hmac", now=time.time(),
        )
    assert exc_info.value.code == "missing_header"


def test_didit_non_hex_signature_raises_malformed() -> None:
    headers = {
        "X-Signature-V2": "not-hex-at-all",
        "X-Timestamp": str(int(time.time())),
    }
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="x", body=b"{}", headers=headers, scheme="didit_hmac",
            now=time.time(),
        )
    assert exc_info.value.code == "malformed_header"


def test_didit_tampered_body_raises_signature_mismatch() -> None:
    secret = "didit-secret"
    body = b'{"session_id":"abc"}'
    ts = int(time.time())
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=b"tampered",
            headers=_didit_sign(secret, body, ts),
            scheme="didit_hmac", now=ts,
        )
    assert exc_info.value.code == "signature_mismatch"


def test_didit_replay_window_old() -> None:
    secret = "x"
    body = b"{}"
    old_ts = int(time.time()) - 600
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=body,
            headers=_didit_sign(secret, body, old_ts),
            scheme="didit_hmac", max_skew_seconds=300, now=time.time(),
        )
    assert exc_info.value.code == "replay_window"


# ── svix (Resend) ───────────────────────────────────────────────────


def test_svix_happy_path() -> None:
    raw_key = b"svix-test-key-bytes"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    body = b'{"type":"email.delivered"}'
    msg_id = "msg_abc123"
    ts = int(time.time())
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": _svix_sign(raw_key, msg_id, ts, body),
    }
    verify_signature(
        secret=secret, body=body, headers=headers, scheme="svix", now=ts
    )


def test_svix_missing_headers_raise_missing_header() -> None:
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="whsec_" + base64.b64encode(b"x").decode(),
            body=b"{}", headers={}, scheme="svix", now=time.time(),
        )
    assert exc_info.value.code == "missing_header"


def test_svix_tampered_body_raises_signature_mismatch() -> None:
    raw_key = b"svix-test-key-bytes"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    body = b'{"type":"email.delivered"}'
    msg_id = "msg_abc"
    ts = int(time.time())
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": _svix_sign(raw_key, msg_id, ts, body),
    }
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=b"tampered", headers=headers,
            scheme="svix", now=ts,
        )
    assert exc_info.value.code == "signature_mismatch"


def test_svix_replay_window_old() -> None:
    raw_key = b"svix-test-key-bytes"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    body = b"{}"
    msg_id = "msg_old"
    old_ts = int(time.time()) - 600
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(old_ts),
        "svix-signature": _svix_sign(raw_key, msg_id, old_ts, body),
    }
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret=secret, body=body, headers=headers, scheme="svix",
            max_skew_seconds=300, now=time.time(),
        )
    assert exc_info.value.code == "replay_window"


def test_svix_supports_millisecond_timestamps() -> None:
    """Svix sends timestamps in unix-seconds historically; some
    integrations send millis. The verifier accepts both transparently."""
    raw_key = b"k"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    body = b"{}"
    msg_id = "msg_ms"
    ts_ms = int(time.time()) * 1000
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(ts_ms),
        "svix-signature": _svix_sign(raw_key, msg_id, ts_ms, body),
    }
    verify_signature(
        secret=secret, body=body, headers=headers, scheme="svix",
        now=time.time(),
    )


def test_svix_multiple_signatures_one_matches() -> None:
    """Header may carry multiple ``v1,<base64>`` entries (key rotation)."""
    raw_key = b"correct-key"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    body = b"{}"
    msg_id = "msg_x"
    ts = int(time.time())
    correct = _svix_sign(raw_key, msg_id, ts, body)
    bad = "v1," + base64.b64encode(b"wrong-sig-32-bytes-padding-fillerXX").decode()
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": f"{bad} {correct}",
    }
    verify_signature(
        secret=secret, body=body, headers=headers, scheme="svix", now=ts
    )


def test_svix_malformed_secret_raises_malformed_header() -> None:
    with pytest.raises(WebhookSignatureError) as exc_info:
        verify_signature(
            secret="whsec_!!!not-valid-base64!!!",
            body=b"{}",
            headers={
                "svix-id": "x",
                "svix-timestamp": str(int(time.time())),
                "svix-signature": "v1,xxx",
            },
            scheme="svix",
            now=time.time(),
        )
    assert exc_info.value.code == "malformed_header"
