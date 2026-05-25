"""OTP mailbox tail for the v1.4 Playwright suite.

In e2e mode the portal runs with ``BSS_PORTAL_EMAIL_PROVIDER=logging`` —
``LoggingEmailAdapter`` appends formatted message blocks to
``.dev-mailbox/portal-mailbox.log`` instead of calling Resend. The bind-mount
in ``docker-compose.yml`` puts the file on the host filesystem at
``<repo-root>/.dev-mailbox/portal-mailbox.log`` so tests can read it directly.

The auth flow is canonical real-user (POST /auth/login → portal writes OTP
to the mailbox → user enters OTP). The only e2e shortcut is the read path:
instead of an inbox we tail a file. No middleware bypass.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

OTP_RE = re.compile(r"OTP:\s*(\d{6})")


def latest_otp(
    mailbox_path: Path,
    email: str,
    *,
    subject_contains: str | None = None,
) -> str | None:
    """Return the most recent 6-digit OTP for ``email``, or None.

    Scans the mailbox file from top to bottom; the last block with a
    matching ``To:`` line wins. With ``subject_contains`` set, the block's
    ``Subject:`` header must contain that substring too — used to
    distinguish login OTPs (subject ``Your bss-cli portal login code``)
    from step-up OTPs (subject ``Confirm action: ...``).
    """
    if not mailbox_path.is_file():
        return None
    txt = mailbox_path.read_text(encoding="utf-8")
    otp: str | None = None
    for block in txt.split("=== "):
        if f"To: {email}" not in block:
            continue
        if subject_contains is not None and subject_contains not in block:
            continue
        m = OTP_RE.search(block)
        if m:
            otp = m.group(1)
    return otp


def wait_for_otp(
    mailbox_path: Path,
    email: str,
    *,
    timeout_seconds: float = 5.0,
    poll_interval: float = 0.3,
    subject_contains: str | None = None,
) -> str:
    """Poll the mailbox until an OTP appears for ``email``.

    Raises ``TimeoutError`` if no OTP arrives within ``timeout_seconds``.
    The default 5 s window matches the LoggingEmailAdapter's typical
    write latency (~immediate) with headroom for fs-cache flush.

    ``subject_contains`` flows through to :func:`latest_otp` for filtering
    by subject — required when the mailbox already has older OTPs for the
    same address (e.g. step-up after a login).
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        otp = latest_otp(mailbox_path, email, subject_contains=subject_contains)
        if otp:
            return otp
        time.sleep(poll_interval)
    raise TimeoutError(
        f"no OTP in {mailbox_path} for {email} within {timeout_seconds}s"
        + (f" (subject contains {subject_contains!r})" if subject_contains else "")
    )
