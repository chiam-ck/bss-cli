"""OPERATOR.md + settings.toml loader with mtime-based hot-reload.

Two operator-editable files, both at ``.bss-cli/`` relative to the repo
root:

* ``OPERATOR.md`` — human-written persona + house rules. Plain
  markdown. Prepended verbatim to every cockpit system prompt by
  :mod:`bss_cockpit.prompts.build_cockpit_prompt`.
* ``settings.toml`` — machine-tunable, non-secret operator preference.
  Validated by Pydantic on load. Hot-reloaded on mtime change.

Both files are operator-local (gitignored). The first ``current()`` call
auto-bootstraps from the committed ``.template`` siblings if the
actuals are missing — so a fresh checkout works without manual setup.

Doctrine notes (per phases/V0_13_0.md):

* The mtime check runs per ``current()`` invocation. Cheap (one
  ``stat``); no watchdog thread. Editing the file in ``$EDITOR`` and
  closing it makes the next REPL turn pick it up.
* If ``settings.toml`` has invalid TOML or fails Pydantic validation,
  the loader keeps serving the last good view and logs a warning.
  This avoids a typo in the operator's editor bricking the cockpit.
* Secrets MUST NOT live in ``settings.toml``. The split is doctrine:
  operator preference here, infrastructure secrets in ``.env``.
* No write API. ``current()`` is read-only; the WebUI ``/settings``
  POST handlers in PR8 own the write side and call into a small
  helper there (not exposed publicly so accidental writes from
  business logic can't bypass the validation gate).
"""

from __future__ import annotations

import os
import shutil
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from bss_clock import now as clock_now
from pydantic import BaseModel, Field, ValidationError

log = structlog.get_logger(__name__)


# v0.13.1 — actor is hardcoded. The cockpit runs single-operator-by-
# design behind a secure perimeter; the audit attribution name is
# always ``"operator"``. Removing it from settings.toml eliminates a
# whole class of cross-surface drift (REPL writing under one actor,
# browser filtering by another) and matches the "secure perimeter"
# trust story — no per-user identity story to preserve.
OPERATOR_ACTOR: str = "operator"


# Embedded default templates. The container deployment may not have
# the repo's .template files on disk (only Python code is bundled in
# the wheel). When the autobootstrap can't find a .template sibling,
# it falls back to writing these constants. Keeps cockpit boots
# resilient regardless of how the package landed (uv workspace, wheel,
# or Docker image with an unmounted .bss-cli/).
_DEFAULT_OPERATOR_MD = """\
# Operator persona

I am the operator. I run a small MVNO on BSS-CLI and use this cockpit daily.

## House rules

- Use SGD with two-decimal precision in all money references.
- Default to terse, action-first replies. Render ASCII tables/cards inline when listing things.
- For destructive actions, propose first with a one-line summary and wait for `/confirm`.
- Escalations stay on the v0.12 list (fraud, billing dispute, regulator complaint,
  identity recovery, bereavement) — open a case via `case.open` for those, do not auto-resolve.

## Defaults

- Currency: SGD
- Tone: factual, dry, no upsell
"""

_DEFAULT_SETTINGS_TOML = """\
[llm]
model = "google/gemma-4-26b-a4b-it"
temperature = 0.2

[cockpit]
allow_destructive_default = false

[ports]
csr_portal = 9002

[dev_service_urls]
"""


# ─────────────────────────────────────────────────────────────────────
# Pydantic schema for settings.toml
# ─────────────────────────────────────────────────────────────────────


class _LlmSection(BaseModel):
    model: str | None = None
    temperature: float = 0.2


class _CockpitSection(BaseModel):
    allow_destructive_default: bool = False


class _PortsSection(BaseModel):
    csr_portal: int = 9002


class CockpitSettings(BaseModel):
    """Validated view of ``.bss-cli/settings.toml``.

    Sections are discrete to keep validation errors locatable in the
    operator's editor (a typo under ``[llm]`` says so by section name,
    not just by key).

    v0.13.1 — the ``[operator].actor`` field is gone; cockpit actor is
    hardcoded to ``OPERATOR_ACTOR = "operator"``. The settings file is
    now purely tunable preference, no identity claims.
    """

    llm: _LlmSection = _LlmSection()
    cockpit: _CockpitSection = _CockpitSection()
    ports: _PortsSection = _PortsSection()
    # Free-form per-service URL overrides. Empty by default.
    dev_service_urls: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class CockpitConfig:
    """Snapshot of the two operator files. Returned by ``current()``."""

    operator_md: str
    settings: CockpitSettings
    last_loaded_at: datetime
    operator_md_path: Path
    settings_path: Path


# ─────────────────────────────────────────────────────────────────────
# Loader + cache
# ─────────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """Locate the .bss-cli/ directory relative to the package install.

    The package lives under ``packages/bss-cockpit/`` so ``parents[3]``
    of this file is the repo root in a workspace-install layout.
    """
    return Path(__file__).resolve().parents[3]


def _bss_cli_dir() -> Path:
    """Where to find OPERATOR.md + settings.toml.

    Resolution order:
      1. ``BSS_COCKPIT_DIR`` env var (absolute path; deployed cockpit
         containers set this to a bind-mounted writable volume).
      2. ``<repo_root>/.bss-cli`` for workspace dev.
    """
    override = os.environ.get("BSS_COCKPIT_DIR", "").strip()
    if override:
        return Path(override)
    return _repo_root() / ".bss-cli"


@dataclass
class _Cache:
    config: CockpitConfig | None = None
    operator_mtime: float = 0.0
    settings_mtime: float = 0.0


_cache = _Cache()
_lock = threading.Lock()


_EMBEDDED_DEFAULTS: dict[str, str] = {
    "OPERATOR.md": _DEFAULT_OPERATOR_MD,
    "settings.toml": _DEFAULT_SETTINGS_TOML,
}


def _autobootstrap_if_missing(path: Path, template_suffix: str = ".template") -> None:
    """Materialize ``path`` from a sibling .template, or from an
    embedded package default if no .template is present.

    Idempotent: returns silently if ``path`` already exists. Logs the
    bootstrap so operators see what happened on first start. Also
    creates the parent directory if it doesn't exist (deployed
    containers point ``BSS_COCKPIT_DIR`` at a fresh empty volume).
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    template = path.with_name(path.name + template_suffix)
    if template.exists():
        shutil.copy2(template, path)
        log.info(
            "cockpit.config.autobootstrap",
            target=str(path),
            source=str(template),
        )
        return
    embedded = _EMBEDDED_DEFAULTS.get(path.name)
    if embedded is not None:
        path.write_text(embedded, encoding="utf-8")
        log.info(
            "cockpit.config.autobootstrap_embedded",
            target=str(path),
            note="no .template sibling found; wrote package default",
        )


def _load_from_disk(
    operator_md_path: Path, settings_path: Path
) -> CockpitConfig:
    """Read both files, validate, return a fresh ``CockpitConfig``.

    Raises any IO / validation error to the caller. ``current()`` is
    the public entry point — it catches and falls back on prior good.
    """
    operator_md = operator_md_path.read_text(encoding="utf-8")
    raw = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    settings_obj = CockpitSettings.model_validate(raw)
    return CockpitConfig(
        operator_md=operator_md,
        settings=settings_obj,
        last_loaded_at=clock_now(),
        operator_md_path=operator_md_path,
        settings_path=settings_path,
    )


def current(*, root: Path | None = None) -> CockpitConfig:
    """Return the current ``CockpitConfig``, reloading on mtime change.

    First-call autobootstraps both files from ``.template`` siblings.
    Subsequent calls stat both files; if either's mtime has advanced,
    re-read + re-validate. On a parse/validation failure, log a warning
    and keep serving the last good view (the operator's editor typo
    must not brick the REPL).

    ``root`` overrides the auto-located ``.bss-cli/`` (used in tests).
    """
    bss_cli = (root if root is not None else _bss_cli_dir())
    operator_md_path = bss_cli / "OPERATOR.md"
    settings_path = bss_cli / "settings.toml"

    _autobootstrap_if_missing(operator_md_path)
    _autobootstrap_if_missing(settings_path)

    op_stat = operator_md_path.stat()
    cf_stat = settings_path.stat()

    with _lock:
        cached = _cache.config
        if (
            cached is not None
            and op_stat.st_mtime <= _cache.operator_mtime
            and cf_stat.st_mtime <= _cache.settings_mtime
        ):
            return cached

        try:
            fresh = _load_from_disk(operator_md_path, settings_path)
        except (
            tomllib.TOMLDecodeError,
            ValidationError,
            OSError,
        ) as exc:
            if cached is None:
                # No prior good — surface the failure. The operator
                # has to fix the file before the cockpit boots.
                raise
            log.warning(
                "cockpit.config.reload_failed",
                error=f"{type(exc).__name__}: {exc}",
                serving_last_good=str(cached.last_loaded_at),
            )
            return cached

        _cache.config = fresh
        _cache.operator_mtime = op_stat.st_mtime
        _cache.settings_mtime = cf_stat.st_mtime
        return fresh


def reset_cache() -> None:
    """Clear the cache. Tests use this between cases."""
    with _lock:
        _cache.config = None
        _cache.operator_mtime = 0.0
        _cache.settings_mtime = 0.0


def write_operator_md(content: str, *, root: Path | None = None) -> None:
    """Persist new OPERATOR.md content. WebUI helper (PR8 caller).

    Validation: non-empty after strip. Anything fancier (markdown
    lint) is out of scope.
    """
    if not content.strip():
        raise ValueError("OPERATOR.md cannot be empty")
    bss_cli = (root if root is not None else _bss_cli_dir())
    bss_cli.mkdir(parents=True, exist_ok=True)
    (bss_cli / "OPERATOR.md").write_text(content, encoding="utf-8")
    # Force a reload on the next current() call.
    reset_cache()


def write_settings_toml(content: str, *, root: Path | None = None) -> CockpitSettings:
    """Persist new settings.toml content; validate first; reset cache.

    Returns the validated ``CockpitSettings`` so the WebUI can echo it
    back. Raises ``tomllib.TOMLDecodeError`` or ``ValidationError`` on
    bad input — the WebUI surfaces the message in its 400 page.
    """
    raw = tomllib.loads(content)
    validated = CockpitSettings.model_validate(raw)
    bss_cli = (root if root is not None else _bss_cli_dir())
    bss_cli.mkdir(parents=True, exist_ok=True)
    (bss_cli / "settings.toml").write_text(content, encoding="utf-8")
    reset_cache()
    return validated
