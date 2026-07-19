"""bss-self-serve — customer-facing signup + post-login portal (v0.4+).

A thin FastAPI + Jinja + HTMX skin over BSS-CLI's domain services.

* **(v0.4)** First shipped as an LLM-mediated signup demo: every write
  went through the orchestrator's ``astream_once`` and the browser
  watched a streaming agent log as the chain ran.
* **(v0.10+)** Post-login customer self-serve carved out as direct-API.
* **(v0.11+)** The signup funnel joins the direct-write side. Every
  page in this portal — signup chain, post-login dashboard, top-up,
  COF management, eSIM redownload, plan change, profile, billing,
  cancel — calls ``bss-clients`` directly. The chat surface is the
  one route that stays orchestrator-mediated when it lands.

See ``phases/V0_11_0.md`` for the v0.11 doctrine consolidation and
``DECISIONS.md`` 2026-04-27 for the rationale.
"""
