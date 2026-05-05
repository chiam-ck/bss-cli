"""bss-cockpit — operator-cockpit Conversation store + config + prompt builder.

v0.13 introduces a unified cockpit owned by the operator. The CLI REPL
is the canonical surface; the browser is a thin veneer over the same
Postgres-backed Conversation store. Either surface can write a turn;
the other resumes seamlessly via ``--session SES-...`` or
``/cockpit/SES-...``.

Public API (PR2 — Conversation store):

* :class:`Conversation` — handle on one cockpit session. Open / resume
  / list / append (user|assistant|tool) / transcript_text / reset /
  close. Plus pending-destructive set/consume and customer-focus pin.
* :class:`ConversationStore` — Postgres-backed singleton. Hung off
  ``app.state`` (portal lifespan) or set up at module import (REPL).
* :class:`ConversationSummary` — row shape returned by ``list_for``.
* :class:`PendingDestructive` — payload returned when ``/confirm``
  consumes the in-flight propose row.
* :func:`configure_store` — register the process-wide default store
  the :class:`Conversation` classmethods delegate to.

Future PRs (per phases/V0_13_0.md):

* PR3 lands :mod:`bss_cockpit.config` (OPERATOR.md + settings.toml +
  hot-reload) and :mod:`bss_cockpit.prompts` (build_cockpit_prompt).
"""

from __future__ import annotations

from .config import (
    OPERATOR_ACTOR,
    CockpitConfig,
    CockpitSettings,
    current,
    reset_cache,
    write_operator_md,
    write_settings_toml,
)
from .conversation import (
    Conversation,
    ConversationMessage,
    ConversationStore,
    ConversationSummary,
    PendingDestructive,
    configure_store,
)
from .postprocess import (
    knowledge_called,
    strip_channel_markup,
    strip_reasoning_leakage,
)
from .prompts import build_cockpit_prompt

__all__ = [
    "OPERATOR_ACTOR",
    "CockpitConfig",
    "CockpitSettings",
    "Conversation",
    "ConversationMessage",
    "ConversationStore",
    "ConversationSummary",
    "PendingDestructive",
    "build_cockpit_prompt",
    "configure_store",
    "current",
    "knowledge_called",
    "reset_cache",
    "strip_channel_markup",
    "strip_reasoning_leakage",
    "write_operator_md",
    "write_settings_toml",
]
