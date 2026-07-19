"""QR PNG generator — smoke test: we get a base64 PNG data URI."""

from __future__ import annotations

import base64
import io

from bss_self_serve.qrpng import activation_qr_data_uri
from PIL import Image


def test_activation_qr_data_uri_produces_valid_png() -> None:
    uri = activation_qr_data_uri("LPA:1$smdp.example$abc-123-def")
    assert uri.startswith("data:image/png;base64,")
    payload = uri.split(",", 1)[1]
    decoded = base64.b64decode(payload)
    img = Image.open(io.BytesIO(decoded))
    assert img.format == "PNG"
    assert img.size[0] > 50  # sanity — not a 1x1 pixel


def test_activation_qr_data_uri_stable_for_same_input() -> None:
    a = activation_qr_data_uri("LPA:1$smdp.example$abc-123")
    b = activation_qr_data_uri("LPA:1$smdp.example$abc-123")
    assert a == b  # deterministic
