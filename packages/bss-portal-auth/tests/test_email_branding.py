"""v1.8 — emails follow the operator branding (name + theme palette).

Pins the doctrine seams: adapters resolve bss_branding.current() per
SEND (hot-reload, no restart); every color in the rendered HTML comes
from the active ThemePalette (no stray hex — a doctrine-check grep
enforces the source side, this test the rendered side); brand name and
mark are html-escaped into the hand-built HTML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import bss_branding
import pytest
from bss_branding import THEMES
from bss_portal_auth.email import LoggingEmailAdapter, ResendEmailAdapter


@pytest.fixture
def branding_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BSS_BRANDING_DIR", str(tmp_path))
    for var in ("BSS_BRAND_NAME", "BSS_BRAND_THEME", "BSS_BRAND_MARK"):
        monkeypatch.delenv(var, raising=False)
    bss_branding.reset_cache()
    yield tmp_path
    bss_branding.reset_cache()


@pytest.fixture
def fake_resend(monkeypatch):
    sent: list[dict] = []

    class _Fake:
        api_key = ""

        class Emails:
            @staticmethod
            def send(params):
                sent.append(params)
                return {"id": f"msg_test_{len(sent)}"}

    _Fake._sent = sent
    monkeypatch.setitem(sys.modules, "resend", _Fake)
    return _Fake


def _set_branding(root: Path, body: str) -> None:
    (root / "settings.toml").write_text(body, encoding="utf-8")
    bss_branding.reset_cache()


def test_custom_brand_in_subject_and_html(branding_root, fake_resend) -> None:
    _set_branding(
        branding_root,
        '[branding]\nbrand_name = "Kopi Mobile"\ntheme = "amber-crt"\nmark = "▲"\n',
    )
    a = ResendEmailAdapter(api_key="re_test", from_address="x <n@x>")
    a.send_login("ada@example.sg", "424242", "https://x/m")
    params = fake_resend._sent[0]
    assert params["subject"] == "Your Kopi Mobile sign-in code"
    assert "Kopi Mobile" in params["html"]
    assert "▲" in params["html"]
    assert "Kopi Mobile — sign in" in params["text"]
    # Theme colors follow amber-crt, not phosphor.
    assert THEMES["amber-crt"].accent in params["html"]
    assert THEMES["phosphor"].accent not in params["html"]
    # Product attribution stays literal in the outer footer.
    assert "powered by bss-cli" in params["html"]


def test_brand_change_applies_per_send_without_restart(branding_root, fake_resend) -> None:
    a = ResendEmailAdapter(api_key="re_test", from_address="x <n@x>")
    a.send_login("ada@example.sg", "111111", "https://x/m")
    _set_branding(branding_root, '[branding]\nbrand_name = "AcmeTel"\n')
    a.send_login("ada@example.sg", "222222", "https://x/m")
    first, second = fake_resend._sent
    assert first["subject"] == "Your bss-cli sign-in code"
    assert second["subject"] == "Your AcmeTel sign-in code"


def test_all_rendered_hex_comes_from_active_palette(branding_root, fake_resend) -> None:
    _set_branding(branding_root, '[branding]\ntheme = "ice"\n')
    a = ResendEmailAdapter(api_key="re_test", from_address="x <n@x>")
    a.send_renewal_reminder(
        "ada@example.sg",
        plan_name="Standard",
        msisdn="91110001",
        amount="25.00",
        currency="SGD",
        renewal_date="5 Aug 2026",
    )
    html_body = fake_resend._sent[0]["html"]
    palette_hex = {
        getattr(THEMES["ice"], f)
        for f in (
            "bg",
            "bg_elev",
            "bg_inset",
            "bg_code",
            "fg",
            "fg_muted",
            "fg_dim",
            "accent",
            "accent_bright",
            "accent_dim",
            "accent_amber",
            "accent_error",
            "border",
            "border_strong",
            "on_accent",
        )
    }
    for hex_color in set(re.findall(r"#[0-9a-fA-F]{6}", html_body)):
        assert hex_color.lower() in palette_hex, f"stray hex {hex_color}"


def test_brand_name_html_escaped(branding_root, fake_resend, monkeypatch) -> None:
    """A hostile brand name must never land unescaped in email HTML.
    The validators reject <> in marks, but brand_name only limits
    length — the render seam is the escape point."""
    monkeypatch.setenv("BSS_BRAND_NAME", "<script>alert(1)</script>x")
    bss_branding.reset_cache()
    a = ResendEmailAdapter(api_key="re_test", from_address="x <n@x>")
    a.send_login("ada@example.sg", "424242", "https://x/m")
    html_body = fake_resend._sent[0]["html"]
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


def test_logging_adapter_subject_carries_brand_and_e2e_substring(branding_root, tmp_path: Path) -> None:
    _set_branding(branding_root, '[branding]\nbrand_name = "Kopi Mobile"\n')
    mailbox = tmp_path / "mailbox.log"
    a = LoggingEmailAdapter(mailbox)
    a.send_login("ada@example.sg", "424242", "https://x/m")
    text = mailbox.read_text(encoding="utf-8")
    assert "Subject: Your Kopi Mobile portal login code" in text
    # e2e helpers match this substring — it must survive any brand.
    assert "portal login code" in text
