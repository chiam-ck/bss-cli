"""Email adapter — pluggable delivery channel for OTPs and magic links.

v0.14 ships:

* ``LoggingEmailAdapter`` — writes the OTP + magic link to a file
  (``BSS_PORTAL_DEV_MAILBOX_PATH``). Default in dev. ``tail -f`` the
  file in dev to "receive" mail.
* ``NoopEmailAdapter`` — for tests. Tests do NOT tail the dev-mailbox
  file (they would race across cases); they use
  ``bss_portal_auth.test_helpers.last_login_codes(adapter, email)``
  instead.
* ``ResendEmailAdapter`` — v0.14 production adapter. Sync HTTP call
  via the ``resend`` Python SDK; structured logging records the
  Resend ``msg_*`` id and latency for forensic correlation with
  inbound webhook events (``/webhooks/resend`` → ``integrations.webhook_event``).
* ``SmtpEmailAdapter`` — explicitly NOT implemented. Stub raises
  NotImplementedError. Reserved for post-v0.16.

The portal selects the adapter via ``BSS_PORTAL_EMAIL_PROVIDER`` env
(renamed from ``BSS_PORTAL_EMAIL_ADAPTER`` in v0.14; old name still
read with a DeprecationWarning until v0.16). Anything other than
the listed values raises at startup.
"""

from __future__ import annotations

import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Protocol

import structlog
from bss_clock import now as clock_now

log = structlog.get_logger(__name__)


# ── HTML email template (matches the self-serve portal's vibe) ─────────
#
# Dark phosphor-green theme matching ``bss-portal-ui/portal_base.css``.
# Inline styles only — gmail/outlook/apple-mail strip <style> blocks.
# Table-based outer layout because email clients still distrust flexbox.
# Web-safe font stack with monospace fallback for the OTP block.
#
# The template is intentionally small: a header brand line, a heading,
# one paragraph, an OTP block, an optional CTA button, and a footnote.
# No images, no remote resources — keeps it compact and avoids
# tracking-pixel paranoia from receiving servers.

_EMAIL_BG = "#0e1014"          # body bg (matches --bg)
_EMAIL_CARD = "#171a20"        # card bg (--bg-elev)
_EMAIL_INSET = "#1f232b"       # OTP block bg (--bg-inset)
_EMAIL_FG = "#d8d8d4"          # primary text (--fg)
_EMAIL_MUTED = "#8a8f99"       # muted text (--fg-muted)
_EMAIL_DIM = "#5a5e66"         # dim text (--fg-dim)
_EMAIL_ACCENT = "#74d535"      # phosphor green (--accent)
_EMAIL_ACCENT_DIM = "#4d8a22"  # accent-dim
_EMAIL_BORDER = "#2a2e38"      # border

_FONT_SANS = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "Helvetica, Arial, sans-serif"
)
_FONT_MONO = (
    "ui-monospace, 'SF Mono', 'JetBrains Mono', "
    "Menlo, Consolas, 'Liberation Mono', monospace"
)


# Humanized labels for the v0.10 SENSITIVE_ACTION_LABELS set. Renders
# in the step-up email subject + body so customers see "Change your
# name" instead of "name_update". Keys must match
# ``portals/self-serve/.../security.py:SENSITIVE_ACTION_LABELS``.
# Unknown labels render as title-cased snake → space (graceful fallback;
# adding an entry here is a one-line PR if a future label needs polish).
_ACTION_LABEL_HUMAN: dict[str, str] = {
    "vas_purchase": "VAS purchase",
    "payment_method_add": "Add a payment method",
    "payment_method_remove": "Remove a payment method",
    "payment_method_set_default": "Set default payment method",
    "subscription_terminate": "Cancel your subscription",
    "email_change": "Change your email",
    "phone_update": "Update your phone number",
    "address_update": "Update your address",
    "name_update": "Change your name",
    "plan_change_schedule": "Schedule a plan change",
    "plan_change_cancel": "Cancel a scheduled plan change",
}


def _humanize_action_label(label: str) -> str:
    """Map ``name_update`` → ``"Change your name"``; fall back to
    ``Title Case`` for unknown labels so the email still reads."""
    if label in _ACTION_LABEL_HUMAN:
        return _ACTION_LABEL_HUMAN[label]
    return label.replace("_", " ").capitalize()


def _render_notification_email(
    *,
    preheader: str,
    heading: str,
    intro: str,
    highlight: str,
    highlight_sub: str | None,
    cta_label: str | None,
    cta_url: str | None,
    footnote: str,
) -> str:
    """Same OTP-vibe template, no OTP — the bordered green block carries
    a non-secret highlight (renewal amount, plan name, etc.) instead.

    v0.18 — used by the upcoming-renewal reminder emitted by the
    subscription service's renewal worker. The visual hierarchy
    (header brand, heading, intro, highlight block, optional CTA,
    footnote) matches the auth emails so the inbox stays consistent.

    ``highlight`` is the prominent monospaced phrase (e.g. ``"SGD 25"``).
    ``highlight_sub`` is an optional smaller line under it (e.g.
    ``"renews 5 Jun 2026"``); pass ``None`` to omit.
    """
    cta_html = ""
    if cta_label and cta_url:
        cta_html = (
            f'<tr><td align="center" style="padding: 8px 0 24px 0;">'
            f'<a href="{cta_url}" '
            f'style="display:inline-block;'
            f'background:{_EMAIL_ACCENT};color:#0e1014;'
            f'font-family:{_FONT_SANS};font-weight:600;font-size:14px;'
            f'text-decoration:none;padding:11px 24px;border-radius:6px;'
            f'border:1px solid {_EMAIL_ACCENT_DIM};">{cta_label}</a>'
            f'</td></tr>'
        )

    sub_html = ""
    if highlight_sub:
        sub_html = (
            f'<div style="margin-top:8px;font-family:{_FONT_SANS};'
            f'font-size:13px;color:{_EMAIL_MUTED};">{highlight_sub}</div>'
        )

    return (
        '<!doctype html>\n'
        '<html><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<meta name="supported-color-schemes" content="dark light">'
        f'<title>{heading}</title>'
        '</head>'
        f'<body style="margin:0;padding:0;background:{_EMAIL_BG};'
        f'color:{_EMAIL_FG};font-family:{_FONT_SANS};">'
        f'<div style="display:none;max-height:0;overflow:hidden;'
        f'opacity:0;color:transparent;">{preheader}</div>'
        '<table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" border="0" style="background:{_EMAIL_BG};">'
        '<tr><td align="center" style="padding: 32px 16px;">'
        '<table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" border="0" '
        f'style="max-width:480px;background:{_EMAIL_CARD};'
        f'border:1px solid {_EMAIL_BORDER};border-radius:8px;'
        f'overflow:hidden;">'
        f'<tr><td style="padding:18px 24px;border-bottom:1px solid '
        f'{_EMAIL_BORDER};font-family:{_FONT_MONO};font-size:13px;">'
        f'<span style="color:{_EMAIL_ACCENT};font-weight:700;">▶</span>'
        f'<span style="color:{_EMAIL_FG};font-weight:600;'
        ' margin-left:8px;">bss-cli</span>'
        f'<span style="color:{_EMAIL_MUTED};margin-left:8px;">'
        ' / self-serve portal</span>'
        '</td></tr>'
        f'<tr><td style="padding:28px 24px 12px 24px;">'
        f'<h1 style="margin:0 0 12px 0;font-size:20px;line-height:1.3;'
        f'color:{_EMAIL_FG};font-weight:600;">{heading}</h1>'
        f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.5;'
        f'color:{_EMAIL_FG};">{intro}</p>'
        '</td></tr>'
        # Highlight block — same border + green color as the OTP block,
        # but content is non-secret (amount, plan, etc.).
        '<tr><td align="center" style="padding: 0 24px 16px 24px;">'
        f'<div style="display:inline-block;background:{_EMAIL_INSET};'
        f'border:1px solid {_EMAIL_BORDER};border-radius:6px;'
        f'padding:14px 22px;font-family:{_FONT_MONO};'
        f'font-size:22px;font-weight:600;'
        f'color:{_EMAIL_ACCENT};">{highlight}{sub_html}</div>'
        '</td></tr>'
        + cta_html +
        f'<tr><td style="padding: 12px 24px 24px 24px;">'
        f'<p style="margin:0;font-family:{_FONT_SANS};font-size:12px;'
        f'line-height:1.5;color:{_EMAIL_MUTED};">{footnote}</p>'
        '</td></tr>'
        '</table>'
        f'<table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" border="0" style="max-width:480px;'
        f'margin-top:16px;">'
        f'<tr><td align="center" style="font-family:{_FONT_MONO};'
        f'font-size:11px;color:{_EMAIL_DIM};">'
        '— sent by BSS-CLI · transactional only —'
        '</td></tr>'
        '</table>'
        '</td></tr></table>'
        '</body></html>'
    )


def _render_email(
    *,
    preheader: str,
    heading: str,
    intro: str,
    otp: str,
    cta_label: str | None,
    cta_url: str | None,
    footnote: str,
) -> str:
    """Build a self-serve-portal-vibed transactional HTML email.

    The ``preheader`` is the hidden snippet some clients show in the
    inbox preview. Keep it short and informative — most clients
    truncate at ~90 chars.

    ``intro`` may contain inline ``<strong>`` for emphasis on action
    labels (step-up flow); other HTML is not sanitized — callers
    construct the string from controlled inputs.

    ``cta_label`` + ``cta_url`` are paired — pass both or neither.
    Step-up emails skip the CTA (OTP-only) because there's no neutral
    page to land on.
    """
    cta_html = ""
    if cta_label and cta_url:
        cta_html = (
            f'<tr><td align="center" style="padding: 8px 0 24px 0;">'
            f'<a href="{cta_url}" '
            f'style="display:inline-block;'
            f'background:{_EMAIL_ACCENT};color:#0e1014;'
            f'font-family:{_FONT_SANS};font-weight:600;font-size:14px;'
            f'text-decoration:none;padding:11px 24px;border-radius:6px;'
            f'border:1px solid {_EMAIL_ACCENT_DIM};">{cta_label}</a>'
            f'</td></tr>'
        )

    return (
        '<!doctype html>\n'
        '<html><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<meta name="supported-color-schemes" content="dark light">'
        f'<title>{heading}</title>'
        '</head>'
        f'<body style="margin:0;padding:0;background:{_EMAIL_BG};'
        f'color:{_EMAIL_FG};font-family:{_FONT_SANS};">'
        # Hidden preheader for inbox preview.
        f'<div style="display:none;max-height:0;overflow:hidden;'
        f'opacity:0;color:transparent;">{preheader}</div>'
        '<table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" border="0" style="background:{_EMAIL_BG};">'
        '<tr><td align="center" style="padding: 32px 16px;">'
        # Card.
        '<table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" border="0" '
        f'style="max-width:480px;background:{_EMAIL_CARD};'
        f'border:1px solid {_EMAIL_BORDER};border-radius:8px;'
        f'overflow:hidden;">'
        # Header.
        f'<tr><td style="padding:18px 24px;border-bottom:1px solid '
        f'{_EMAIL_BORDER};font-family:{_FONT_MONO};font-size:13px;">'
        f'<span style="color:{_EMAIL_ACCENT};font-weight:700;">▶</span>'
        f'<span style="color:{_EMAIL_FG};font-weight:600;'
        ' margin-left:8px;">bss-cli</span>'
        f'<span style="color:{_EMAIL_MUTED};margin-left:8px;">'
        ' / self-serve portal</span>'
        '</td></tr>'
        # Body.
        f'<tr><td style="padding:28px 24px 12px 24px;">'
        f'<h1 style="margin:0 0 12px 0;font-size:20px;line-height:1.3;'
        f'color:{_EMAIL_FG};font-weight:600;">{heading}</h1>'
        f'<p style="margin:0 0 20px 0;font-size:15px;line-height:1.5;'
        f'color:{_EMAIL_FG};">{intro}</p>'
        '</td></tr>'
        # OTP block.
        '<tr><td align="center" style="padding: 0 24px 16px 24px;">'
        f'<div style="display:inline-block;background:{_EMAIL_INSET};'
        f'border:1px solid {_EMAIL_BORDER};border-radius:6px;'
        f'padding:14px 22px;font-family:{_FONT_MONO};'
        f'font-size:26px;letter-spacing:6px;font-weight:600;'
        f'color:{_EMAIL_ACCENT};">{otp}</div>'
        '</td></tr>'
        + cta_html +
        # Footnote.
        f'<tr><td style="padding: 12px 24px 24px 24px;">'
        f'<p style="margin:0;font-family:{_FONT_SANS};font-size:12px;'
        f'line-height:1.5;color:{_EMAIL_MUTED};">{footnote}</p>'
        '</td></tr>'
        '</table>'
        # Outer footer.
        f'<table role="presentation" width="100%" cellspacing="0" '
        f'cellpadding="0" border="0" style="max-width:480px;'
        f'margin-top:16px;">'
        f'<tr><td align="center" style="font-family:{_FONT_MONO};'
        f'font-size:11px;color:{_EMAIL_DIM};">'
        '— sent by BSS-CLI · transactional only —'
        '</td></tr>'
        '</table>'
        '</td></tr></table>'
        '</body></html>'
    )


class EmailAdapter(Protocol):
    """Delivery surface used by the auth flows + v0.18 renewal worker."""

    def send_login(self, email: str, otp: str, magic_link: str) -> None: ...
    def send_step_up(self, email: str, otp: str, action_label: str) -> None: ...
    def send_email_change_verification(
        self, new_email: str, otp: str, magic_link: str
    ) -> None: ...
    def send_renewal_reminder(
        self,
        email: str,
        *,
        plan_name: str,
        msisdn: str,
        amount: str,
        currency: str,
        renewal_date: str,
    ) -> None:
        """v0.18 — sent ~24h before next_renewal_at by the subscription
        service's renewal worker.

        Args:
            email: customer's verified email address.
            plan_name: human plan name (``"Standard"``, not ``"PLAN_M"``).
            msisdn: 8-digit MSISDN of the line being renewed.
            amount: pre-formatted decimal string (``"25.00"``).
            currency: ISO-4217 (``"SGD"``).
            renewal_date: human date (``"5 Jun 2026"``).
        """
        ...


class LoggingEmailAdapter:
    """Append-only dev mailbox — one log line per "send", plain text.

    Format is deliberately greppable so the hero scenarios can `tail`
    the file and pick out the most-recent OTP for a given email.
    Production-shape ``LoggingEmailAdapter`` is fine for a small MVNO
    in dev / staging; v0.14 swaps ``ResendEmailAdapter`` in for prod.

    Doctrine: this is the ONLY place where the OTP / magic-link
    plaintext is written anywhere outside the customer's inbox. The
    grep guard `rg 'log\\.(info|debug|warning).*(otp|magic_link|token)'
    packages/bss-portal-auth/` must stay empty — i.e. structlog never
    sees the secret.
    """

    def __init__(self, mailbox_path: str | Path):
        self.path = Path(mailbox_path)
        # Touch parent dir; the file itself is created on first write.
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, lines: list[str]) -> None:
        ts = clock_now().isoformat()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {ts} ===\n")
            for line in lines:
                fh.write(line + "\n")

    def send_login(self, email: str, otp: str, magic_link: str) -> None:
        self._append(
            [
                f"To: {email}",
                "Subject: Your bss-cli portal login code",
                "",
                f"OTP: {otp}",
                f"Magic link: {magic_link}",
                "",
                "Code expires in 15 minutes.",
            ]
        )
        # Structured log carries length only, never the secret itself.
        log.info(
            "portal_auth.email.login_sent",
            adapter="logging",
            email_domain=email.split("@", 1)[-1] if "@" in email else "?",
        )

    def send_step_up(self, email: str, otp: str, action_label: str) -> None:
        self._append(
            [
                f"To: {email}",
                f"Subject: Confirm action: {action_label}",
                "",
                f"OTP: {otp}",
                f"Action: {action_label}",
                "",
                "Code expires in 5 minutes.",
            ]
        )
        log.info(
            "portal_auth.email.step_up_sent",
            adapter="logging",
            action=action_label,
        )

    def send_email_change_verification(
        self, new_email: str, otp: str, magic_link: str
    ) -> None:
        self._append(
            [
                f"To: {new_email}",
                "Subject: Verify your new email for bss-cli",
                "",
                f"OTP: {otp}",
                f"Magic link: {magic_link}",
            ]
        )
        log.info("portal_auth.email.change_sent", adapter="logging")

    def send_renewal_reminder(
        self,
        email: str,
        *,
        plan_name: str,
        msisdn: str,
        amount: str,
        currency: str,
        renewal_date: str,
    ) -> None:
        self._append(
            [
                f"To: {email}",
                f"Subject: Your {plan_name} plan renews on {renewal_date}",
                "",
                f"Plan: {plan_name}",
                f"MSISDN: {msisdn}",
                f"Amount: {currency} {amount}",
                f"Renews on: {renewal_date}",
                "",
                "Card on file will be charged automatically.",
                "No action needed unless you want to switch plans or cancel.",
            ]
        )
        log.info(
            "portal_auth.email.renewal_reminder_sent",
            adapter="logging",
            email_domain=email.split("@", 1)[-1] if "@" in email else "?",
        )


class NoopEmailAdapter:
    """Test adapter — keeps the most recent code for each (email, kind)
    in process memory. Tests inspect via
    ``bss_portal_auth.test_helpers.last_login_codes(email)``.
    """

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, str | datetime]] = {}

    def send_login(self, email: str, otp: str, magic_link: str) -> None:
        self.records[(email, "login")] = {
            "otp": otp,
            "magic_link": magic_link,
            "ts": clock_now(),
        }

    def send_step_up(self, email: str, otp: str, action_label: str) -> None:
        self.records[(email, f"step_up:{action_label}")] = {
            "otp": otp,
            "action_label": action_label,
            "ts": clock_now(),
        }

    def send_email_change_verification(
        self, new_email: str, otp: str, magic_link: str
    ) -> None:
        self.records[(new_email, "email_change")] = {
            "otp": otp,
            "magic_link": magic_link,
            "ts": clock_now(),
        }

    def send_renewal_reminder(
        self,
        email: str,
        *,
        plan_name: str,
        msisdn: str,
        amount: str,
        currency: str,
        renewal_date: str,
    ) -> None:
        self.records[(email, "renewal_reminder")] = {
            "plan_name": plan_name,
            "msisdn": msisdn,
            "amount": amount,
            "currency": currency,
            "renewal_date": renewal_date,
            "ts": clock_now(),
        }


class ResendEmailAdapter:
    """v0.14 production adapter — sends via Resend's HTTPS API.

    Construction validates the API key + sender; instantiation does NOT
    make a network call. The first ``send_*`` triggers the SDK's HTTPS
    call. Sync (the SDK is sync; preserves existing call-site shape).

    Each ``send_*`` call records to structlog with:

    * ``provider="resend"``
    * ``operation`` (one of ``send_login`` / ``send_step_up`` /
      ``send_email_change``)
    * ``provider_call_id`` (the ``msg_*`` id Resend returns)
    * ``latency_ms``
    * ``success`` (bool)
    * ``email_domain`` only — never the full address (PII), never the
      OTP / magic-link.

    The structured log line is the forensic correlation point with
    inbound webhook events (``/webhooks/resend`` → ``email.delivered``,
    ``email.bounced``, etc.). Both share the ``provider_call_id``.

    Forward path (v0.15+): once the adapter Protocol goes async, calls
    will also write a row to ``integrations.external_call``. v0.14
    leaves that for the async migration.
    """

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
    ) -> None:
        if not api_key:
            raise ValueError("ResendEmailAdapter requires a non-empty api_key")
        if not from_address:
            raise ValueError(
                "ResendEmailAdapter requires from_address "
                "(BSS_PORTAL_EMAIL_FROM, e.g. 'BSS-CLI <noreply@mail.example.com>')"
            )
        self._api_key = api_key
        self._from = from_address
        # Lazy import — keep `import bss_portal_auth.email` cheap and avoid
        # forcing the resend SDK on test contexts using NoopEmailAdapter.
        import resend

        resend.api_key = api_key
        self._resend = resend

    # ── public API (sync) ────────────────────────────────────────────

    def send_login(self, email: str, otp: str, magic_link: str) -> None:
        body_html = _render_email(
            preheader=f"Your login code: {otp}. Expires in 15 minutes.",
            heading="Sign in to BSS-CLI",
            intro="Use the code below or click the button to sign in.",
            otp=otp,
            cta_label="Sign in",
            cta_url=magic_link,
            footnote="Code expires in 15 minutes. If you didn't request this, you can ignore this email.",
        )
        body_text = (
            f"BSS-CLI — sign in\n"
            f"\n"
            f"OTP: {otp}\n"
            f"Magic link: {magic_link}\n"
            f"\n"
            f"Code expires in 15 minutes.\n"
            f"If you didn't request this, you can ignore this email."
        )
        self._send(
            operation="send_login",
            to=email,
            subject="Your BSS-CLI sign-in code",
            html=body_html,
            text=body_text,
        )

    def send_step_up(self, email: str, otp: str, action_label: str) -> None:
        human = _humanize_action_label(action_label)
        body_html = _render_email(
            preheader=f"Confirm: {human}. Code: {otp}. Expires in 5 minutes.",
            heading=human,
            intro=(
                "Use the code below to confirm this action. "
                "We're asking because it's a change to your account or "
                "service that we don't want to do without a second check."
            ),
            otp=otp,
            cta_label=None,
            cta_url=None,
            footnote=(
                "Code expires in 5 minutes. If you didn't initiate this "
                "action, ignore this email — no change has been made — "
                "and consider rotating any account credentials you've "
                "reused elsewhere."
            ),
        )
        body_text = (
            f"BSS-CLI — confirm: {human}\n"
            f"\n"
            f"OTP: {otp}\n"
            f"\n"
            f"Code expires in 5 minutes.\n"
            f"If you didn't initiate this action, ignore this email."
        )
        self._send(
            operation="send_step_up",
            to=email,
            subject=f"Confirm: {human}",
            html=body_html,
            text=body_text,
        )

    def send_email_change_verification(
        self, new_email: str, otp: str, magic_link: str
    ) -> None:
        body_html = _render_email(
            preheader=f"Verify your new email. Code: {otp}.",
            heading="Verify your new email",
            intro="Enter this code or click the button to confirm this email address for your BSS-CLI account.",
            otp=otp,
            cta_label="Verify email",
            cta_url=magic_link,
            footnote="If you didn't request an email change, ignore this email and your account stays unchanged.",
        )
        body_text = (
            f"BSS-CLI — verify your new email\n"
            f"\n"
            f"OTP: {otp}\n"
            f"Magic link: {magic_link}\n"
            f"\n"
            f"If you didn't request an email change, ignore this email."
        )
        self._send(
            operation="send_email_change",
            to=new_email,
            subject="Verify your new email for BSS-CLI",
            html=body_html,
            text=body_text,
        )

    def send_renewal_reminder(
        self,
        email: str,
        *,
        plan_name: str,
        msisdn: str,
        amount: str,
        currency: str,
        renewal_date: str,
    ) -> None:
        body_html = _render_notification_email(
            preheader=(
                f"Your {plan_name} plan renews {renewal_date} for "
                f"{currency} {amount}."
            ),
            heading=f"Your {plan_name} plan renews soon",
            intro=(
                f"Your line <strong>{msisdn}</strong> renews on "
                f"<strong>{renewal_date}</strong>. Your card on file "
                "will be charged automatically — no action needed unless "
                "you want to switch plans or cancel."
            ),
            highlight=f"{currency} {amount}",
            highlight_sub=f"renews {renewal_date}",
            cta_label=None,
            cta_url=None,
            footnote=(
                "Want to switch plans, cancel, or update your card? Sign "
                "in to your portal — changes take effect at the next "
                "renewal boundary."
            ),
        )
        body_text = (
            f"BSS-CLI — your {plan_name} plan renews soon\n"
            f"\n"
            f"Line: {msisdn}\n"
            f"Renews on: {renewal_date}\n"
            f"Amount: {currency} {amount}\n"
            f"\n"
            f"Your card on file will be charged automatically. "
            f"No action needed unless you want to switch plans or cancel."
        )
        self._send(
            operation="send_renewal_reminder",
            to=email,
            subject=f"Your {plan_name} plan renews on {renewal_date}",
            html=body_html,
            text=body_text,
        )

    # ── internal ─────────────────────────────────────────────────────

    def _send(
        self,
        *,
        operation: str,
        to: str,
        subject: str,
        html: str,
        text: str,
    ) -> None:
        """Single Resend HTTPS call with structured-log forensic record."""
        params = {
            "from": self._from,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        }
        domain = to.split("@", 1)[-1] if "@" in to else "?"
        start = time.monotonic()
        try:
            result = self._resend.Emails.send(params)
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            log.warning(
                "portal_auth.email.send_failed",
                adapter="resend",
                operation=operation,
                email_domain=domain,
                latency_ms=elapsed,
                error_class=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            raise
        elapsed = int((time.monotonic() - start) * 1000)
        # Resend returns a dict-like with at least an ``id`` field.
        provider_call_id = (
            result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        )
        log.info(
            "portal_auth.email.sent",
            adapter="resend",
            operation=operation,
            email_domain=domain,
            latency_ms=elapsed,
            provider_call_id=provider_call_id,
        )


class SmtpEmailAdapter:
    """Reserved for post-v0.16. Selecting it raises at construction time so
    nothing silently falls back to no-op delivery in production.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "SmtpEmailAdapter is reserved for post-v0.16. Use ResendEmailAdapter "
            "(BSS_PORTAL_EMAIL_PROVIDER=resend) for production, or LoggingEmailAdapter "
            "(BSS_PORTAL_EMAIL_PROVIDER=logging) for dev."
        )

    def send_login(self, email: str, otp: str, magic_link: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def send_step_up(self, email: str, otp: str, action_label: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def send_email_change_verification(
        self, new_email: str, otp: str, magic_link: str
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    def send_renewal_reminder(
        self, email: str, **_kwargs
    ) -> None:  # pragma: no cover
        raise NotImplementedError


def select_adapter(
    name: str,
    mailbox_path: str | Path,
    *,
    resend_api_key: str = "",
    from_address: str = "",
) -> EmailAdapter:
    """Resolve ``BSS_PORTAL_EMAIL_PROVIDER`` -> concrete adapter.

    Unknown values raise — fail fast at portal startup, never silently
    downgrade to no-op delivery.
    """
    name = name.lower()
    if name == "logging":
        return LoggingEmailAdapter(mailbox_path)
    if name == "noop":
        return NoopEmailAdapter()
    if name == "resend":
        if not resend_api_key:
            raise RuntimeError(
                "BSS_PORTAL_EMAIL_PROVIDER=resend requires "
                "BSS_PORTAL_EMAIL_RESEND_API_KEY to be set."
            )
        if not from_address:
            raise RuntimeError(
                "BSS_PORTAL_EMAIL_PROVIDER=resend requires "
                "BSS_PORTAL_EMAIL_FROM to be set "
                "(e.g. 'BSS-CLI <noreply@mail.example.com>')."
            )
        return ResendEmailAdapter(
            api_key=resend_api_key, from_address=from_address
        )
    if name == "smtp":
        # Constructor raises — preserves "fail-fast at startup" doctrine.
        return SmtpEmailAdapter()
    raise RuntimeError(
        f"Unknown BSS_PORTAL_EMAIL_PROVIDER={name!r}; expected "
        "'logging' (default), 'noop' (tests), 'resend' (v0.14+), or "
        "'smtp' (reserved post-v0.16)."
    )


def resolve_provider_name(
    *, provider: str, legacy_adapter: str
) -> str:
    """Reconcile new ``BSS_PORTAL_EMAIL_PROVIDER`` with legacy
    ``BSS_PORTAL_EMAIL_ADAPTER``.

    * Both empty → default to ``"logging"``.
    * Only legacy set → emit DeprecationWarning, use legacy value.
    * Only new set → use it.
    * Both set → use new; warn if they differ (legacy ignored).

    Removed in v0.16. Greppable: this function is the only place that
    reads ``BSS_PORTAL_EMAIL_ADAPTER``.
    """
    new = (provider or "").strip()
    old = (legacy_adapter or "").strip()
    if not new and not old:
        return "logging"
    if not new and old:
        warnings.warn(
            "BSS_PORTAL_EMAIL_ADAPTER is deprecated; rename to "
            "BSS_PORTAL_EMAIL_PROVIDER. Old name removed in v0.16.",
            DeprecationWarning,
            stacklevel=2,
        )
        return old
    if new and old and new != old:
        warnings.warn(
            f"Both BSS_PORTAL_EMAIL_PROVIDER ({new!r}) and "
            f"BSS_PORTAL_EMAIL_ADAPTER ({old!r}) are set with different "
            "values; using BSS_PORTAL_EMAIL_PROVIDER. Remove the old "
            "name from .env to silence this warning.",
            DeprecationWarning,
            stacklevel=2,
        )
    return new
