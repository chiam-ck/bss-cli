"""Fail-fast startup validators for the BSS perimeter token map.

v0.9 introduces ``validate_token_map_present`` (in ``api_token.py``)
which loads + validates a multi-token map. The v0.3-era helper
``validate_api_token_present`` is preserved as a thin alias that
emits a one-time deprecation warning, so existing service ``main.py``
files continue to boot mid-rollout. Both names removed in v1.0.
"""

from __future__ import annotations

import warnings

import structlog

from .api_token import validate_token_map_present

log = structlog.get_logger(__name__)

_DEPRECATION_LOGGED = False


def validate_api_token_present() -> None:
    """Deprecated alias — call ``validate_token_map_present`` instead.

    Behaviour is identical for the single-``BSS_API_TOKEN`` case (v0.3
    deployments). For multi-token deployments the new name returns
    the loaded ``TokenMap``; this alias drops the return for source
    compat. A deprecation warning is emitted once per process so logs
    surface the migration without spamming.
    """
    global _DEPRECATION_LOGGED
    if not _DEPRECATION_LOGGED:
        warnings.warn(
            "validate_api_token_present() is deprecated — call "
            "validate_token_map_present() instead. The old name is removed in v1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        log.info("auth.token.validate.deprecated_alias")
        _DEPRECATION_LOGGED = True
    validate_token_map_present()
