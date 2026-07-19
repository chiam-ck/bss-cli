"""bss-csr — operator cockpit (browser veneer over the v0.13 cockpit
Conversation store).

A small FastAPI + Jinja + HTMX portal on port 9002. The CLI REPL is
the canonical surface; this browser is a thin veneer over the same
Postgres-backed ``cockpit.session``/``cockpit.message``/
``cockpit.pending_destructive`` store. Open ``/`` for the sessions
index, ``/cockpit/<id>`` for a chat thread, ``/case/<id>`` for a
read-only case deep-link.

No login. The cockpit runs single-operator-by-design behind a secure
perimeter; ``actor`` for cockpit turns comes from
``.bss-cli/settings.toml`` via ``bss_cockpit.config.current()``.

Every cockpit write goes through ``astream_once`` in
``routes/cockpit.py`` with the v0.13 ``operator_cockpit`` profile +
named token. The shared agent-log widget, chat-bubble renderers, SSE
plumbing, base CSS, and vendored HTMX live in ``bss_portal_ui``.
"""
