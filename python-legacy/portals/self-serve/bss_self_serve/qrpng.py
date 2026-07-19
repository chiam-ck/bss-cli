"""Render an eSIM LPA activation code as a base64-embeddable PNG.

The v0.1 CLI already ships an ASCII-QR renderer for the terminal
(``cli/bss_cli/renderers/esim.py``). A web portal can do the one
thing a terminal can't — draw a real PNG — so the confirmation page
does exactly that. The QR is served inline via a ``data:image/png``
URI to keep the bundle size flat and avoid a second round-trip.
"""

from __future__ import annotations

import base64
import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def activation_qr_data_uri(lpa_code: str, *, box_size: int = 8, border: int = 2) -> str:
    """Return ``data:image/png;base64,...`` for embedding into ``<img src="">``."""
    return qr_data_uri(lpa_code, box_size=box_size, border=border)


def qr_data_uri(payload: str, *, box_size: int = 8, border: int = 2) -> str:
    """Generic ``data:image/png;base64,...`` QR — used by activation codes
    and v0.15 KYC cross-device handoff URLs alike."""
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0e1014", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
