"""bss-branding — operator branding for BSS-CLI (v1.8).

Read path + palette definitions only. All writes to ``settings.toml``
and the logo file live in ``bss_cockpit.config`` (the v0.13 single
write path, amended v1.8 for the ``[branding]`` section's read side).
"""

from .assets import (
    CONTENT_TYPE_BY_FILENAME,
    CONTENT_TYPES,
    LOGO_FILENAMES,
    MAX_LOGO_BYTES,
    sniff_image_type,
)
from .config import (
    DEFAULT_BRAND_NAME,
    LOGO_SUBDIR,
    BrandingSettings,
    BrandingView,
    branding_dir,
    current,
    file_settings,
    reset_cache,
)
from .css import branding_css_block
from .marks import LOGO_MARKS, validate_mark
from .themes import DEFAULT_THEME_ID, THEMES, ThemePalette

__all__ = [
    "CONTENT_TYPE_BY_FILENAME",
    "CONTENT_TYPES",
    "DEFAULT_BRAND_NAME",
    "DEFAULT_THEME_ID",
    "LOGO_FILENAMES",
    "LOGO_MARKS",
    "LOGO_SUBDIR",
    "MAX_LOGO_BYTES",
    "THEMES",
    "BrandingSettings",
    "BrandingView",
    "ThemePalette",
    "branding_css_block",
    "branding_dir",
    "current",
    "file_settings",
    "reset_cache",
    "sniff_image_type",
    "validate_mark",
]
