"""Read path for the ``[branding]`` section of ``.bss-cli/settings.toml``.

Same contract as :mod:`bss_cockpit.config` — one ``stat()`` per
``current()`` call, reload on mtime change, keep serving the last good
view on a parse/validation error — with two deliberate deltas
(doctrine, phases/V1_8_0.md):

* **Never bootstraps files.** ``bss_cockpit.config`` owns creating
  ``settings.toml``; this module only reads whatever is there.
* **Never crashes on absence.** A container without the ``.bss-cli/``
  mount (or a fresh checkout before first cockpit boot) gets pure
  defaults + env overrides. Branding must never take a service down.

Writes stay in ``bss_cockpit.config`` (``write_branding_settings`` /
``write_branding_logo``) — the v0.13 "single write path" seam is
unchanged; v1.8 only amends the *read* side.

Env overrides (``BSS_BRAND_NAME`` / ``BSS_BRAND_THEME`` /
``BSS_BRAND_MARK``) are applied inside ``current()`` on every call.
This is deliberately different from the v0.9 "tokens load once" rule:
that rule is about secrets; branding is non-secret preference whose
whole point is hot-reload.
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel, ValidationError, field_validator

from .assets import CONTENT_TYPE_BY_FILENAME
from .marks import validate_mark
from .themes import DEFAULT_THEME_ID, THEMES, ThemePalette

log = structlog.get_logger(__name__)

DEFAULT_BRAND_NAME = "bss-cli"

# Subdirectory of the branding dir where the uploaded logo lives.
LOGO_SUBDIR = "branding"


class BrandingSettings(BaseModel):
    """Validated view of the ``[branding]`` TOML table.

    Also embedded as a section of ``bss_cockpit.config.CockpitSettings``
    so the cockpit's whole-document validation covers it.
    """

    brand_name: str = DEFAULT_BRAND_NAME
    theme: str = DEFAULT_THEME_ID
    mark: str = "$"
    # "" = no uploaded logo. Only the fixed filenames the upload
    # handler writes are legal — never a free-form path.
    logo_image: str = ""

    @field_validator("brand_name")
    @classmethod
    def _brand_name_sane(cls, v: str) -> str:
        name = v.strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("brand_name must be 1-40 characters")
        return name

    @field_validator("theme")
    @classmethod
    def _theme_known(cls, v: str) -> str:
        if v not in THEMES:
            raise ValueError(f"unknown theme {v!r} — pick one of: {', '.join(THEMES)}")
        return v

    @field_validator("mark")
    @classmethod
    def _mark_valid(cls, v: str) -> str:
        return validate_mark(v)

    @field_validator("logo_image")
    @classmethod
    def _logo_image_fixed_name(cls, v: str) -> str:
        if v and v not in CONTENT_TYPE_BY_FILENAME:
            raise ValueError("logo_image must be one of: " + ", ".join(CONTENT_TYPE_BY_FILENAME) + " (or empty)")
        return v


@dataclass(frozen=True)
class BrandingView:
    """Resolved branding snapshot handed to renderers.

    ``logo_path`` is exists-checked at load; ``logo_version`` is the
    file's integer mtime for ``?v=`` cache-busting (0 = no logo).
    """

    brand_name: str
    theme: ThemePalette
    mark: str
    logo_path: Path | None
    logo_version: int


def _repo_root() -> Path:
    # packages/bss-branding/bss_branding/config.py → repo root.
    return Path(__file__).resolve().parents[3]


def branding_dir() -> Path:
    """Where to find ``settings.toml`` + ``branding/logo.*``.

    Resolution order:
      1. ``BSS_BRANDING_DIR`` (containers that only need branding —
         self-serve portal, subscription service — mount ``.bss-cli/``
         read-only and point this at it).
      2. ``BSS_COCKPIT_DIR`` (the csr container already mounts the same
         directory writable for OPERATOR.md/settings.toml).
      3. ``<repo_root>/.bss-cli`` for workspace dev.
    """
    for var in ("BSS_BRANDING_DIR", "BSS_COCKPIT_DIR"):
        override = os.environ.get(var, "").strip()
        if override:
            return Path(override)
    return _repo_root() / ".bss-cli"


@dataclass
class _Cache:
    settings: BrandingSettings | None = None
    mtime: float = 0.0
    announced: bool = False


_cache = _Cache()
_lock = threading.Lock()


def _load_settings(path: Path) -> BrandingSettings:
    """Parse the ``[branding]`` table only; other sections are the
    cockpit's business. Raises to the caller."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return BrandingSettings.model_validate(raw.get("branding", {}))


def _cached_settings(settings_path: Path) -> BrandingSettings:
    try:
        mtime = settings_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    with _lock:
        if not _cache.announced:
            _cache.announced = True
            log.info(
                "branding.dir_resolved",
                dir=str(settings_path.parent),
                settings_present=mtime > 0.0,
            )

        if _cache.settings is not None and mtime == _cache.mtime:
            return _cache.settings

        if mtime == 0.0:
            fresh = BrandingSettings()  # absent file → pure defaults
        else:
            try:
                fresh = _load_settings(settings_path)
            except (tomllib.TOMLDecodeError, ValidationError, OSError) as exc:
                if _cache.settings is None:
                    # No prior good — defaults, not a crash. Branding
                    # must never take a service down.
                    log.warning(
                        "branding.load_failed_using_defaults",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    fresh = BrandingSettings()
                else:
                    log.warning(
                        "branding.reload_failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    return _cache.settings

        _cache.settings = fresh
        _cache.mtime = mtime
        return fresh


def _apply_env_overrides(settings: BrandingSettings) -> BrandingSettings:
    overrides: dict[str, str] = {}
    for field, var in (
        ("brand_name", "BSS_BRAND_NAME"),
        ("theme", "BSS_BRAND_THEME"),
        ("mark", "BSS_BRAND_MARK"),
    ):
        value = os.environ.get(var, "").strip()
        if value:
            overrides[field] = value
    if not overrides:
        return settings
    try:
        return BrandingSettings.model_validate({**settings.model_dump(), **overrides})
    except ValidationError as exc:
        log.warning(
            "branding.env_override_invalid_ignored",
            error=f"{type(exc).__name__}: {exc}",
        )
        return settings


def current(*, root: Path | None = None) -> BrandingView:
    """Return the resolved ``BrandingView``, hot-reloading on change.

    Cheap enough to call per render / per email send — one or two
    ``stat()`` calls on the happy path. ``root`` overrides the
    auto-located directory (tests).
    """
    base = root if root is not None else branding_dir()
    settings = _apply_env_overrides(_cached_settings(base / "settings.toml"))

    logo_path: Path | None = None
    logo_version = 0
    if settings.logo_image:
        candidate = base / LOGO_SUBDIR / settings.logo_image
        try:
            logo_version = int(candidate.stat().st_mtime)
            logo_path = candidate
        except OSError:
            # Configured but missing on disk (e.g. container without
            # the mount): degrade to the glyph, don't 500.
            logo_path = None
            logo_version = 0

    return BrandingView(
        brand_name=settings.brand_name,
        theme=THEMES[settings.theme],
        mark=settings.mark,
        logo_path=logo_path,
        logo_version=logo_version,
    )


def reset_cache() -> None:
    """Clear the cache. Tests use this between cases."""
    with _lock:
        _cache.settings = None
        _cache.mtime = 0.0
        _cache.announced = False
