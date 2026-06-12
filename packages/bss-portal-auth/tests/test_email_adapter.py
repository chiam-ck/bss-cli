"""Email adapter — selection, LoggingEmailAdapter file IO, NoopEmailAdapter records."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bss_portal_auth import (
    LoggingEmailAdapter,
    NoopEmailAdapter,
    SmtpEmailAdapter,
    select_adapter,
)
from bss_portal_auth.email import ResendEmailAdapter, resolve_provider_name
from bss_portal_auth.test_helpers import last_login_codes, last_step_up_code


def test_noop_adapter_records_login_otp_and_magic_link():
    adapter = NoopEmailAdapter()
    adapter.send_login("ada@example.sg", "424242", "MAGIC_LINK_TOKEN")
    rec = last_login_codes(adapter, "ada@example.sg")
    assert rec["otp"] == "424242"
    assert rec["magic_link"] == "MAGIC_LINK_TOKEN"


def test_noop_adapter_scopes_step_up_by_action_label():
    adapter = NoopEmailAdapter()
    adapter.send_step_up("ada@example.sg", "111111", "subscription.terminate")
    adapter.send_step_up("ada@example.sg", "222222", "payment.remove_method")
    assert last_step_up_code(adapter, "ada@example.sg", "subscription.terminate") == "111111"
    assert last_step_up_code(adapter, "ada@example.sg", "payment.remove_method") == "222222"


def test_logging_adapter_writes_otp_and_magic_link_to_mailbox(tmp_path):
    mailbox = tmp_path / "mailbox.log"
    adapter = LoggingEmailAdapter(mailbox)
    adapter.send_login("ada@example.sg", "424242", "MAGIC_LINK_TOKEN")
    body = mailbox.read_text()
    assert "ada@example.sg" in body
    assert "OTP: 424242" in body
    assert "MAGIC_LINK_TOKEN" in body


def test_logging_adapter_appends_each_send(tmp_path):
    mailbox = tmp_path / "mailbox.log"
    adapter = LoggingEmailAdapter(mailbox)
    adapter.send_login("a@x", "111111", "ml1")
    adapter.send_login("a@x", "222222", "ml2")
    body = mailbox.read_text()
    assert "111111" in body and "222222" in body
    # Two separator lines = two sends.
    assert body.count("===") >= 4  # opening + closing per send


def test_smtp_adapter_construct_raises():
    """v0.14: must NOT silently fall back to a no-op SMTP adapter."""
    with pytest.raises(NotImplementedError, match="reserved for post-v0.16"):
        SmtpEmailAdapter()


def test_select_adapter_logging(tmp_path):
    a = select_adapter("logging", tmp_path / "m.log")
    assert isinstance(a, LoggingEmailAdapter)


def test_select_adapter_noop(tmp_path):
    a = select_adapter("noop", tmp_path / "m.log")
    assert isinstance(a, NoopEmailAdapter)


def test_select_adapter_smtp_fails_fast(tmp_path):
    with pytest.raises(NotImplementedError):
        select_adapter("smtp", tmp_path / "m.log")


def test_select_adapter_unknown_value_raises(tmp_path):
    with pytest.raises(RuntimeError, match="Unknown BSS_PORTAL_EMAIL_PROVIDER"):
        select_adapter("postal-pigeon", tmp_path / "m.log")


# ── ResendEmailAdapter (v0.14) ──────────────────────────────────────


@pytest.fixture
def fake_resend(monkeypatch):
    """Stub the ``resend`` module so tests don't make real HTTP calls."""
    fake = MagicMock()
    fake.api_key = ""

    sent: list[dict] = []

    class _Emails:
        @staticmethod
        def send(params):
            sent.append(params)
            return {"id": "msg_test_" + str(len(sent))}

    fake.Emails = _Emails
    fake._sent = sent  # type: ignore[attr-defined]

    monkeypatch.setitem(__import__("sys").modules, "resend", fake)
    return fake


def test_resend_adapter_requires_api_key():
    with pytest.raises(ValueError, match="non-empty api_key"):
        ResendEmailAdapter(api_key="", from_address="x@y")


def test_resend_adapter_requires_from_address():
    with pytest.raises(ValueError, match="from_address"):
        ResendEmailAdapter(api_key="re_test", from_address="")


def test_resend_adapter_send_login_calls_sdk_with_correct_params(fake_resend):
    a = ResendEmailAdapter(api_key="re_test", from_address="BSS-CLI <noreply@x>")
    a.send_login("ada@example.sg", "424242", "https://x/m")
    assert len(fake_resend._sent) == 1
    params = fake_resend._sent[0]
    assert params["from"] == "BSS-CLI <noreply@x>"
    assert params["to"] == ["ada@example.sg"]
    assert "424242" in params["html"] and "424242" in params["text"]
    assert "https://x/m" in params["html"] and "https://x/m" in params["text"]
    assert params["subject"] == "Your BSS-CLI sign-in code"


def test_resend_adapter_send_step_up_includes_action_label(fake_resend):
    """Real SENSITIVE_ACTION_LABELS are snake_case; rendered as a
    humanized string (``subscription_terminate`` →
    ``"Cancel your subscription"``)."""
    a = ResendEmailAdapter(api_key="re_test", from_address="x@y")
    a.send_step_up("ada@example.sg", "111111", "subscription_terminate")
    params = fake_resend._sent[0]
    assert "Cancel your subscription" in params["subject"]
    assert "Cancel your subscription" in params["html"]
    assert "111111" in params["text"]
    # Raw snake_case label must NOT leak — operators see the humanized form.
    assert "subscription_terminate" not in params["subject"]


def test_resend_adapter_send_step_up_unknown_label_falls_back(fake_resend):
    """Unknown labels (future additions, malformed) render via title-case
    fallback so the email still arrives readable."""
    a = ResendEmailAdapter(api_key="re_test", from_address="x@y")
    a.send_step_up("ada@example.sg", "222222", "future_unknown_action")
    params = fake_resend._sent[0]
    assert "Future unknown action" in params["subject"]


def test_resend_adapter_send_email_change_targets_new_address(fake_resend):
    a = ResendEmailAdapter(api_key="re_test", from_address="x@y")
    a.send_email_change_verification("new@example.sg", "999999", "https://x/v")
    params = fake_resend._sent[0]
    assert params["to"] == ["new@example.sg"]
    assert "999999" in params["html"]
    assert "https://x/v" in params["html"]


def test_resend_adapter_propagates_send_failure(fake_resend):
    """Resend SDK raises on auth/quota errors. Adapter must not swallow."""

    class _BoomEmails:
        @staticmethod
        def send(params):
            raise RuntimeError("invalid api key")

    fake_resend.Emails = _BoomEmails
    a = ResendEmailAdapter(api_key="re_test", from_address="x@y")
    with pytest.raises(RuntimeError, match="invalid api key"):
        a.send_login("ada@example.sg", "424242", "https://x/m")


def test_select_adapter_resend_requires_api_key(tmp_path):
    with pytest.raises(RuntimeError, match="BSS_PORTAL_EMAIL_RESEND_API_KEY"):
        select_adapter(
            "resend", tmp_path / "m.log",
            resend_api_key="", from_address="x@y",
        )


def test_select_adapter_resend_requires_from_address(tmp_path):
    with pytest.raises(RuntimeError, match="BSS_PORTAL_EMAIL_FROM"):
        select_adapter(
            "resend", tmp_path / "m.log",
            resend_api_key="re_test", from_address="",
        )


def test_select_adapter_resend_constructs_when_configured(fake_resend, tmp_path):
    a = select_adapter(
        "resend", tmp_path / "m.log",
        resend_api_key="re_test", from_address="BSS <x@y>",
    )
    assert isinstance(a, ResendEmailAdapter)


# ── resolve_provider_name (v0.14 → v0.16 deprecation alias) ──────


def test_resolve_provider_defaults_to_logging():
    assert resolve_provider_name(provider="", legacy_adapter="") == "logging"


def test_resolve_provider_uses_legacy_with_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="rename to"):
        out = resolve_provider_name(provider="", legacy_adapter="resend")
    assert out == "resend"


def test_resolve_provider_uses_new_when_only_new_set():
    out = resolve_provider_name(provider="resend", legacy_adapter="")
    assert out == "resend"


def test_resolve_provider_uses_new_and_warns_when_both_set_differently():
    with pytest.warns(DeprecationWarning, match="Both"):
        out = resolve_provider_name(provider="resend", legacy_adapter="logging")
    assert out == "resend"


def test_resolve_provider_does_not_warn_when_both_set_to_same_value():
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        # Should NOT warn — values agree.
        out = resolve_provider_name(provider="resend", legacy_adapter="resend")
    assert out == "resend"
