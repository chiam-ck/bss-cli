"""v1.8 branding write path — tomlkit round-trip + logo lifecycle.

The write side of [branding] lives here (bss_cockpit.config), the read
side in bss-branding. These tests pin the seam: comment preservation
for the operator's other sections, whole-document validation before
disk, and both caches resetting so a write is immediately visible via
bss_branding.current()."""

from __future__ import annotations

from pathlib import Path

import bss_branding
import pytest
from bss_branding import BrandingSettings
from bss_cockpit.config import (
    remove_branding_logo,
    reset_cache,
    write_branding_logo,
    write_branding_settings,
    write_settings_toml,
)
from pydantic import ValidationError

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64

SETTINGS_WITH_COMMENTS = """\
[llm]
# my precious hand-tuned model choice
model = "deepseek/deepseek-v4-pro"
temperature = 0.2

[cockpit]
allow_destructive_default = false

[dev_service_urls]
# crm = "http://localhost:8002"
"""


@pytest.fixture(autouse=True)
def _clean_caches():
    reset_cache()
    bss_branding.reset_cache()
    yield
    reset_cache()
    bss_branding.reset_cache()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "settings.toml").write_text(SETTINGS_WITH_COMMENTS, encoding="utf-8")
    (tmp_path / "OPERATOR.md").write_text("# op\n", encoding="utf-8")
    return tmp_path


def test_write_preserves_other_sections_comments(root: Path) -> None:
    write_branding_settings(BrandingSettings(brand_name="Kopi Mobile", theme="ice"), root=root)
    text = (root / "settings.toml").read_text(encoding="utf-8")
    assert "# my precious hand-tuned model choice" in text
    assert '# crm = "http://localhost:8002"' in text
    assert 'brand_name = "Kopi Mobile"' in text
    assert 'theme = "ice"' in text


def test_write_visible_via_bss_branding_current(root: Path) -> None:
    assert bss_branding.current(root=root).theme.id == "phosphor"
    write_branding_settings(BrandingSettings(theme="magenta"), root=root)
    assert bss_branding.current(root=root).theme.id == "magenta"


def test_write_rejects_invalid_theme_before_disk(root: Path) -> None:
    before = (root / "settings.toml").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        write_branding_settings(
            BrandingSettings.model_construct(  # bypass model validation
                brand_name="x", theme="hotdog-stand", mark="$", logo_image=""
            ),
            root=root,
        )
    assert (root / "settings.toml").read_text(encoding="utf-8") == before


def test_write_bootstraps_missing_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    write_branding_settings(BrandingSettings(theme="paper"), root=target)
    assert bss_branding.current(root=target).theme.id == "paper"


def test_raw_settings_write_resets_branding_cache(root: Path) -> None:
    assert bss_branding.current(root=root).theme.id == "phosphor"
    write_settings_toml(
        SETTINGS_WITH_COMMENTS + '\n[branding]\ntheme = "amber-crt"\n',
        root=root,
    )
    assert bss_branding.current(root=root).theme.id == "amber-crt"


def test_logo_lifecycle(root: Path) -> None:
    filename = write_branding_logo(PNG_BYTES, root=root)
    assert filename == "logo.png"
    assert (root / "branding" / "logo.png").exists()
    view = bss_branding.current(root=root)
    assert view.logo_path is not None
    assert view.logo_version > 0

    # Replacing with a JPEG removes the stale PNG.
    assert write_branding_logo(JPEG_BYTES, root=root) == "logo.jpg"
    assert not (root / "branding" / "logo.png").exists()
    assert (root / "branding" / "logo.jpg").exists()

    remove_branding_logo(root=root)
    assert not (root / "branding" / "logo.jpg").exists()
    assert bss_branding.current(root=root).logo_path is None


def test_logo_upload_preserves_other_branding_fields(root: Path) -> None:
    write_branding_settings(
        BrandingSettings(brand_name="Kopi Mobile", theme="ice", mark="▲"),
        root=root,
    )
    write_branding_logo(PNG_BYTES, root=root)
    view = bss_branding.current(root=root)
    assert view.brand_name == "Kopi Mobile"
    assert view.theme.id == "ice"
    assert view.logo_path is not None


def test_logo_rejects_oversize_and_wrong_type(root: Path) -> None:
    with pytest.raises(ValueError, match="cap"):
        write_branding_logo(PNG_BYTES + b"\x00" * bss_branding.MAX_LOGO_BYTES, root=root)
    with pytest.raises(ValueError, match="PNG, JPEG or WebP"):
        write_branding_logo(b"<svg></svg>", root=root)
    # Nothing written, nothing configured.
    assert bss_branding.current(root=root).logo_path is None
