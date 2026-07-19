"""eSIM activation renderer — ICCID/IMSI/MSISDN + QR ASCII of the LPA string."""

from __future__ import annotations

from typing import Any

import qrcode

from ._box import format_iccid, format_msisdn

_STATUS_DOTS = {
    "prepared": "● PREPARED",
    "downloaded": "● DOWNLOADED",
    "activated": "● ACTIVATED",
    "suspended": "○ SUSPENDED",
    "released": "○ RELEASED",
    "recycled": "○ RECYCLED",
}


def _qr_ascii(payload: str, border: int = 1) -> list[str]:
    """Return the QR as ASCII rows using two-row block characters.

    Pairing two QR rows into one terminal row halves the vertical size
    (was ~40+ rows; now ~22-26 for an L0-PA payload). Each terminal row
    encodes two QR rows: top half (▀), bottom half (▄), both (█), neither (' ').
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2 == 1:
        # pad with an empty bottom row so pairs line up
        matrix.append([False] * len(matrix[0]))

    lines: list[str] = []
    for r in range(0, len(matrix), 2):
        top = matrix[r]
        bot = matrix[r + 1]
        row_chars: list[str] = []
        for c in range(len(top)):
            t, b = top[c], bot[c]
            if t and b:
                row_chars.append("█")
            elif t and not b:
                row_chars.append("▀")
            elif b and not t:
                row_chars.append("▄")
            else:
                row_chars.append(" ")
        lines.append("".join(row_chars))
    return lines


def _status_strip(status: str) -> str:
    """Header strip showing prepared/activated/suspended state."""
    return _STATUS_DOTS.get(status.lower(), f"● {status.upper()}")


def _redact_id(value: str, *, show_full: bool) -> str:
    """Show the last 4 digits + dots prefix unless show_full=True."""
    if show_full or not value or value == "—":
        return value
    s = str(value)
    if len(s) <= 4:
        return s
    return "•" * (len(s) - 4) + s[-4:]


def render_esim_activation(
    activation: dict[str, Any],
    *,
    show_full: bool = False,
) -> str:
    """Render the eSIM activation card with ASCII QR.

    Expects ``activation`` shaped like
    ``{iccid, imsi, msisdn, activationCode, status?}``.
    Pass ``show_full=True`` to reveal full ICCID + IMSI; default
    redacts to last-4 because the card is often shown to humans
    (CSR screens, demo screenshots) and the identifiers should not
    leak past last-4 in those contexts.
    """
    iccid = activation.get("iccid") or "—"
    imsi = activation.get("imsi") or "—"
    msisdn = activation.get("msisdn") or ""
    code = activation.get("activationCode") or ""
    status = activation.get("status") or "prepared"

    lpa = code if code.startswith("LPA:") else f"LPA:1${code}" if code else ""

    qr_lines = _qr_ascii(lpa or "LPA:1$smdp.bss-cli.local$UNKNOWN")
    qr_width = max(len(line) for line in qr_lines)

    iccid_disp = format_iccid(iccid) if show_full else _redact_id(iccid, show_full=show_full)
    if imsi and len(imsi) >= 5 and show_full:
        imsi_disp = f"{imsi[:3]} {imsi[3:5]} {imsi[5:]}".strip()
    else:
        imsi_disp = _redact_id(imsi, show_full=show_full)

    width = 64
    inner = width - 2

    def _row(s: str = "") -> str:
        return "│" + s.ljust(inner) + "│"

    title = f"eSIM Activation  {_status_strip(status)}"
    header = ["┌─ " + title + " " + "─" * max(0, inner - len(title) - 3) + "┐"]
    meta = [
        _row(),
        _row(f"  ICCID    {iccid_disp}"),
        _row(f"  IMSI     {imsi_disp}"),
        _row(f"  MSISDN   {format_msisdn(msisdn)}"),
        _row(),
    ]

    # QR block centered with side label
    body = [_row("  Scan with your device camera:")]
    body.append(_row())
    pad_left = max(2, (inner - qr_width) // 2)
    for q in qr_lines:
        line = " " * pad_left + q
        body.append(_row(line[:inner]))

    footer = [
        _row(),
        _row("  Or enter the LPA code manually:"),
        _row(f"  {lpa[:inner - 4]}"),
    ]
    if not show_full:
        footer.append(_row())
        footer.append(_row("  (use --show-full to reveal full ICCID + IMSI)"))
    footer.append("└" + "─" * inner + "┘")

    return "\n".join(header + meta + body + footer)
