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


def latest_otp(mailbox_path: Path, email: str) -> str | None:
    """Return the most recent 6-digit OTP for ``email``, or None.

    Scans the mailbox file from top to bottom; the last block with a
    matching ``To:`` line wins. Returns None if the file doesn't exist
    yet (portal hasn't written anything) or no block matches.
    """
    if not mailbox_path.is_file():
        return None
    txt = mailbox_path.read_text(encoding="utf-8")
    otp: str | None = None
    for block in txt.split("=== "):
        if f"To: {email}" in block:
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
) -> str:
    """Poll the mailbox until an OTP appears for ``email``.

    Raises ``TimeoutError`` if no OTP arrives within ``timeout_seconds``.
    The default 5 s window matches the LoggingEmailAdapter's typical
    write latency (~immediate) with headroom for fs-cache flush.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        otp = latest_otp(mailbox_path, email)
        if otp:
            return otp
        time.sleep(poll_interval)
    raise TimeoutError(
        f"no OTP in {mailbox_path} for {email} within {timeout_seconds}s"
    )
