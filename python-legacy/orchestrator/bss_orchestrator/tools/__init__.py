"""LLM tool surface — importing this package populates ``TOOL_REGISTRY``.

Every tool module decorates its functions with ``@register(...)`` at import
time. Importing the sub-modules below is therefore sufficient to assemble
the full registry; downstream code should consume ``TOOL_REGISTRY`` /
``get_tool`` / ``list_tools`` rather than reaching into modules directly.

v0.12: ``mine_wrappers`` registers the ``customer_self_serve`` profile's
``*.mine`` / ``*_for_me`` tools alongside the canonical ones, and
``_profiles.validate_profiles()`` runs at import time as a fail-fast
check that no profile drifts and no wrapper grew a forbidden parameter.
"""

from __future__ import annotations

from . import (  # noqa: F401  (imports for side-effect: @register population)
    case,
    catalog,
    customer,
    inventory,
    knowledge,
    mine_wrappers,
    ops,
    order,
    payment,
    port_request,
    promo,
    provisioning,
    som,
    subscription,
    ticket,
    usage,
)
from ._profiles import TOOL_PROFILES, get_profile, validate_profiles
from ._registry import TOOL_REGISTRY, get_tool, list_tools, register

# Fail fast at import time so deploys catch profile drift / wrapper
# signature regressions before the first chat turn rather than via a
# leaked transcript at runtime.
validate_profiles()

__all__ = [
    "TOOL_PROFILES",
    "TOOL_REGISTRY",
    "get_profile",
    "get_tool",
    "list_tools",
    "register",
    "validate_profiles",
]
