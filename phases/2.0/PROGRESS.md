# Migration Progress Log

Running log of the phases/2.0 Rust port. One entry per work session. The plan
docs (`00`–`04`) are the *design*; this file is the *state*.

Branch: `2.0`. Workspace: [`../../rust/`](../../rust/).

---

## ⇢ HANDOFF (next session) — REPL visual parity (s18b-polish)

**State:** the REPL (`bss` no subcommand) is functionally live (s18b) + two fixes:
localhost URL defaults (`c04f193`) and the renderer-less-tool prose fix (`ba905fe`,
`f2e241d` s18b). It boots, drives real turns, renders ASCII tool cards (catalog,
subscription, etc.), stages `/confirm`. **Gap the operator flagged:** it looks plain
next to the Python REPL. Python has: a big ASCII logo, a **bordered branded banner
panel**, each reply in a **green bordered `bss ai` panel**, **markdown-rendered
replies** (headings/bold/tables), a **full colored slash-hint line**, and inline
tool-call `[info]` log lines. The Rust REPL currently prints plain text seams.

**Task: port the REPL presentation** (`rust/cli/src/repl.rs`, add a `repl_ui.rs`).
All facts gathered:
- **Logo:** `_LOGO` at `cli/bss_cli/repl.py:123` (the `██████╗` block art) — copy
  verbatim, print in the accent color.
- **Slash hints:** `_SLASH_HELP` at `repl.py:132` — `/sessions list · /new [label] ·
  /switch SES · /reset · /focus CUST · /360 · /ports · /confirm · /config edit ·
  /operator edit · /help · /exit` (green verbs). Full banner in `_render_banner`
  (`repl.py:151`): logo (bold accent) + `brand_name` + tagline + actor/model/session/
  focus meta + hints, all in a Rich `Panel`.
- **Reply panel:** Python does `Panel(Markdown(final_text), title="bss ai",
  border_style="green")`. Currently the Rust REPL prints `bss ai\n{text}` plain.
- **Branding (Rust):** `bss_branding::current(None) -> BrandingView { brand_name,
  theme: ThemePalette, mark, .. }`. `ThemePalette` has `accent` (hex, phosphor
  `"#74d535"`), `rich_accent` (name, `"green"`), `accent_amber`, `fg`, `fg_dim`, …
  Default theme = phosphor (green). Use the hex for ANSI truecolor
  (`\x1b[38;2;R;G;Bm`).
- **Width:** `crossterm` is already in the lock (via reedline) — add it as a direct
  CLI dep and use `crossterm::terminal::size()` (fallback 100) for panel width.
- **Approach:** ANSI-truecolor helpers from the theme hex; a `panel(title, body_lines,
  border_color, width)` box-drawer (rounded `╭─╮│╰╯`, title in the top rule); a light
  markdown→ANSI renderer (headings → bold+accent, `**bold**`, `` `code` `` → dim,
  `- ` bullets → `•`, and ideally pipe-tables — the LLM replies lean on these). Wire:
  banner panel at start/switch, reply in a green `bss ai` panel, full slash hints.
  The tool cards already look right (they go through `render_tool_result`); leave them.
- The `[info] knowledge.search …` lines in Python are structlog from the tool call —
  nice-to-have, lower priority than the panels/markdown.

**After the polish:** resume the REPL feature slices — **s18c** (`/sessions /new
/switch /reset /focus`), **s18d** (`/360 /ports /config /operator` + the list-intent
intercept `_maybe_intent_match`/`_drive_intent_turn`). Then the **scenario engine**
(P7 exit gate), **onboard**, **bss-seed/bss-admin** wiring.

**Also open (this session's work, all pushed + CI green):**
- Relay/idempotency hardening DONE: SOM idempotency (`2333f72`), MqChannel
  reconnection (`00b25f1`), provisioning inbox dedup (`2629a43`, migration 0032 +
  applied to tech-vm). See [[rust-provisioning-activation-loop]] +
  [[rust-mq-relay-no-reconnect]]. **Deploy note:** the MqChannel fix is in the shared
  `bss-events` crate — only provisioning-sim + payment + som were rebuilt during
  verification; a **full-stack rebuild** is needed for it to be live on
  com/subscription/crm/catalog/mediation/rating (additive, so the mixed state is safe).
- **Consumer-loop re-subscription** is still a follow-up (the reconnection fix covers
  the publish path, not long-lived consumers whose channel dies).
- **E2E order verified end to end** (ORD-0098 → SUB-0089 active). Reminder:
  Claude's verification flips `BSS_PAYMENT_PROVIDER`→mock + KYC→prebaked and restores
  after (stripe/didit are the operator's for manual testing) — see
  [[verification-uses-mock-providers]]. `.env` service URLs use compose names; the CLI
  now defaults to `localhost:800X` so `bss` works from the host with no exports.

---

## Tagging discipline (2.0)

Every phase gets an **annotated** git tag when — and only when — its exit criteria
in [`03-PHASES.md`](03-PHASES.md) are met (parity harness green on the mixed stack,
golden diffs clean, `cargo fmt` + `clippy -D warnings` + `test` green). The tag is
the "phase done" gate — consistent with the repo's "verify first, commit after /
one commit per phase minimum."

**Scheme — phase pre-releases of the final `v2.0.0`:**

| Tag | Cut / gated on |
|---|---|
| `v2.0.0-phase.0` | Foundations: 7 platform crates + CI + golden rig; hello-world conformance passes |
| `v2.0.0-phase.1` | rating cut over (pilot); per-service playbook written |
| `v2.0.0-phase.2` | mediation + provisioning-sim + som cut over |
| `v2.0.0-phase.3` | catalog + com cut over |
| `v2.0.0-phase.4` | **bilingual resting point** — all 9 services Rust; portals/orch/CLI still Python. Shippable pause point (strategy §5); re-measure motto #6 for the service plane vs [`05-BASELINE.md`](05-BASELINE.md) |
| `v2.0.0-phase.5` | orchestrator lib + knowledge + cockpit-core (no deployable cutover; fixture-parity green) |
| `v2.0.0-phase.6` | portals (self-serve, csr) cut over |
| `v2.0.0-phase.7` | CLI + REPL + scenarios cut over |
| `v2.0.0` | **final cutover** — all-Rust, Alembic retired, 14-day soak passed (Phase 8) |

SemVer ordering holds: `2.0.0-phase.0 < 2.0.0-phase.1 < … < 2.0.0-phase.7 < 2.0.0`
(numeric pre-release identifiers order numerically; any pre-release precedes the
release). The major bump to `2.0.0` marks the platform rewrite even though wire
contracts are frozen (§3) — the migration is behaviour-frozen, not API-versioned.

**Mechanics:**
- `git tag -a v2.0.0-phase.N -m "<phase>: <what cut over>; exit criteria met (<evidence>)"` — annotated so the message records the exit-criteria evidence.
- Tag the commit on `2.0` that *completes* the phase (post-merge if the phase ran on a feature branch). **Mid-phase commits are never tagged** — e.g. this scaffold commit is *not* `phase.0`; that tag waits until all seven crates + CI + the golden rig are done.
- Intra-phase service cutovers (P2 ×3, P4 ×3) are **commits, not tags** (`feat(payment): rust cutover`); the phase tag caps the set. If one service must be pinned for a prod canary, use an incrementing pre-release: `v2.0.0-phase.4.1`, `.2`, `.3`.
- The Python parity baseline stays `v1.8.1` on mainline; every 2.0 tag is `v2.0.0-*`, so they never collide.

---

## Phase 7 — CLI + REPL + scenario engine — 🚧 IN PROGRESS (started 2026-07-18)

The `bss` binary: a clap command tree (~19 groups, mostly thin `bss-clients`
calls), `bss ask` (single-shot LLM), the reedline REPL (the canonical operator
cockpit), and the scenario engine (the acceptance harness's own driver). Exit:
`bss scenario run-all --tag hero` by the *Rust* runner matches the Python runner
on the all-Rust stack. `trace.*` / `knowledge.*` land here (they need the
Jaeger/Audit/PgPool handles the P6 portal bundle omitted).

**s1 — the skeleton (`9939975`).** The clap root, the `.env` bootstrap (walks up
to the repo `.env` so `bss` works without a sourced shell — the REPL needs
`BSS_DB_URL` + the cockpit token as process env), the telemetry root span (the CLI
is the root of every `bss <cmd>` trace), and the `clock` group. `main` owns one
Tokio runtime (init_telemetry's OTLP exporter needs rt-tokio; the client-backed
groups are async) — the Python sync-Typer + asyncio.run-per-command becomes one
process-wide runtime. `clock now` smoke-matches the oracle ISO format.

**s2 — the runtime + catalog (`87f4c9b`).** `Clients::from_env` (the default-token
bundle, `TokenAuthProvider` over `BSS_API_TOKEN`) + `run_safely` (runs each body in
`bss_context::scope` with channel="cli"/actor="cli-user", maps `PolicyViolation` →
red banner + exit 2, other errors → exit 1). First client-backed group. `catalog
vas` keeps the CLI's OWN row format (not the golden `render_vas_list`, which backs
the LLM tool surface), pinned byte-for-byte against the Python f-string.

**s3–s6 — som, usage, subscription, order, prov (`92be141`, `ef9a8dd`, `1f3d9aa`,
`02d6100`).** Seven groups total now: catalog, clock, order, prov, som,
subscription, usage. `list`/`show` reuse the golden renderers; the row-format and
`*-show` JSON-dump commands are hand-ported to match the Python f-strings (widths,
`None`/`True`/`False` rendering, the double-space in the subscription list row).
`run_safely_code` added for `prov fault` (exit 2 on a non-error "no injector" path).

**Owed client gaps closed while porting (all real — no earlier caller needed them):**
`mediation::submit_usage` had dropped Python's `source`/`raw_cdr_ref` (the LLM tool
never set them; `bss usage simulate` stamps `source="cli"`) → added
`submit_usage_full`, key order preserved (D9). Earlier P6c gaps (paged list_*,
transition_case/update_case_priority, list_promotions/admin_retire_offering, the
ticket FSM maps on CrmClient, mediation on the portal bundle) also landed.

**Faithful seams pinned:** the `quote_plus` redirect encoder (P6c), the `{trigger!r}`
single-quote copy, `datetime.fromisoformat().isoformat()` normalisation, and the
usage quantity parser (1GB→1024mb etc.) — each captured from the live oracle.

**s7–s11 — case+ticket, payment+promo, inventory, admin-catalog (`7acbfd9`,
`2822466`, `8f54d4d`, `2ff8978`).** Six more groups + the `admin` parent (catalog
only so far). Both flagged decisions resolved:
- **`payment add-card` (tokenizer):** `bss_orchestrator::tools::payment::local_tokenize_card`
  is now `pub` (returns the ValueError detail as a plain `String`; the write tool
  maps it back to `ToolError::Other{kind:"ValueError"}`). The CLI imports it exactly
  as Python does rather than duplicating sandbox logic — CLAUDE.md "CLI calls the
  orchestrator, nothing more". Added `bss-orchestrator` + `rust_decimal` to the cli
  crate (both needed by `ask`/REPL regardless).
- **`inventory` list + `promo`/`admin catalog` show (rich.Table):** rendered as
  simplified title+header+rows tables — the box-drawing chrome is a **documented CLI
  seam**, but every cell value matches Python's extraction exactly (fallback chains,
  `... or '—'`/`'NULL'`, Python list repr for `applicableOfferingIds`).
- **New client methods (all real gaps):** `payment.cutover_invalidate_mock_tokens`,
  `catalog.unassign_promotion` + `exhaust_promotion`. **`run_safely_promo`** runner
  (promo uniquely maps `NotFound`→exit 2). Shared **`normalize_decimal`** (str(Decimal))
  + **`parse_iso`** (`_parse_iso` seam) helpers in `commands/mod.rs`, unit-tested vs
  the Python isoformat cases. `payment cutover` reproduces `typer.confirm`'s `[y/N]`.

**s12 — branding (`6885537`).** Config-layer (sync, no client): show/themes/set-*.
Writes go through the same gate the cockpit screen uses —
`BrandingSettings::validate` → `bss_cockpit::write_branding_settings` (reject →
`rejected: <err>` exit 1). The rich colour swatches + tables are ANSI-truecolor
presentation seams; show/themes render the palette hexes inline instead.

**s13 — customer (`3dfd438`).** create/list/show, unblocked by the now-`pub`
tokenizer: `customer create --card` tokenises + attaches under `run_safely_code`
(bad PAN after create = partial success → exit 1). show fans out into
`render_customer_360`.

**s14 — trace + `admin reset` (`e473204`, `4256ff1`, `894cb4f`).** Two infra-carrying
groups. **trace** (get/for-order/for-subscription/for-ask/services) reuses the ported
`AuditClient::list_events` + `render_swimlane`; the shared `bss_clients::JaegerClient`
(previously get_trace-only) gained `list_services`/`find_traces`/`latest_ask_trace_id`
(I first duplicated it CLI-local, then folded it back — `4256ff1`). **admin reset**
adds the `AdminClient` (single reset_operational_data POST, 30s timeout) + a
seven-service fan-out with Python's except-ladder error mapping and the
`typer.prompt("Type 'reset' to confirm")` gate.

**s15 — external-calls (`d4e4f53`).** The first CLI group that talks to Postgres
directly rather than through a service HTTP client — the forensic `external_call`
log is cross-provider triage data with no owning service. Wires `bss-db` (the shared
`connect()` pool) + `sqlx` into the CLI crate for the first time. Read-only browser:
`--provider`/`--since`/`--aggregate`/`--month-to-date`/`--limit`/`--failures`, dynamic
WHERE with positional binds, ordered `occurred_at DESC`. The `--since` `<n>{s,m,h,d}`
parser + the `--month-to-date`/`--since` mutual-exclusion + `BSS_DB_URL`-unset all
map to Python's exit 2; the two `rich.Table` outputs (row browser + month summary)
are the documented seam — cell values (`✓`/`✗`, `%m-%d %H:%M:%S`, `type:id` aggregate,
`[:40]` error) match Python. Verified end-to-end against live Postgres (23 rows;
provider/failures/aggregate/since/month-to-date all correct).

**s16 — admin knowledge (`7815114`).** The last thin command group. Mounts as the
third `admin` subcommand (alongside catalog + reset) — `reindex` (walk `INDEXED_PATHS`,
chunk on headings, upsert `knowledge.doc_chunk` via `bss_knowledge::Indexer`), `search`
(Tier-0 FTS debug over `search_fts`, the same shape the cockpit's `knowledge.search`
returns), `list` (paginated `doc_chunk` browse — raw sqlx, `LEFT(content_hash, 8)`).
Reuses the s15 `bss-db`/`sqlx` wiring; `BSS_DB_URL`-unset + missing-`pyproject.toml`
repo-root map to Python's exit 1. The reindex summary line (`✓ reindex complete
files=… added=…`) matches byte-for-byte; the two `rich.Table` outputs are the seam
(the `‹…›` ts_headline match markers are dropped so the snippet cell shows the same
visible words rich would render). Verified end-to-end against live Postgres (reindex
26 files, kind-filtered search + list, no-hits path).

Groups now live (19 — every command group ported): catalog, clock, order, prov, som,
subscription, usage, case, ticket, payment, promo, inventory, admin (catalog + reset +
knowledge), branding, customer, trace, external-calls. **The `bss <subcommand>` surface
is complete.**

**s17 — the shared tool registry + `bss ask` (`1ad0255`, `5bce994`).** The first big
piece. Two commits:

- **s17a (`1ad0255`) — extract the registry.** The full operator tool surface lived
  only inside the CSR portal's private `build_cockpit_registry`, which deliberately
  omitted `trace.*`/`knowledge.*` (no Jaeger/audit/pool handles). New
  `bss_orchestrator::registry`: `RegistryClients` (the nine typed service clients,
  built by each caller with its own auth) + `RegistryExtras` (optional
  Jaeger/audit/knowledge-pool) → `build_registry()`, registering every read + write
  family plus `trace.*`/`knowledge.*` when handles are present. The trace/knowledge
  tool impls already existed (test-pinned) — this only wires them. CSR now delegates
  to the shared builder and passes Jaeger + both audit surfaces, so the cockpit gains
  `trace.*` (the `operator_cockpit` profile already lists them — a closed parity gap,
  not a surface change). Cockpit knowledge stays unwired at its sync build point (the
  FTS pool connects later in `build_state_with_db`).
- **s17b (`5bce994`) — `bss ask`.** Port of `ask.py` + `llm_runner.run_single_shot` →
  `session.ask_once`. Runs one prompt through a fresh agent over the full surface
  (`tool_filter=None`, `SYSTEM_PROMPT`, autonomy Batched) and prints the final
  assistant text (rich.Panel seam). `runtime::build_agent_registry` reuses
  `Clients::from_env` + supplies the extras, so `bss ask` gets the **complete** Python
  surface including trace.* + knowledge.* (knowledge pool gated on
  `BSS_KNOWLEDGE_ENABLED` + `BSS_DB_URL`). Runs inside a `bss_context::scope` with
  `channel="llm"` / `actor="llm-<model>"` (port of `use_llm_context`). Missing
  `BSS_LLM_API_KEY` and a loop `Error` both map to Python's exit-1 "LLM unavailable".
  Verified live: a clock round-trip and a `knowledge.search` citation ([HANDBOOK §8.4])
  both correct. **Known telemetry gap:** no explicit `bss.ask` span yet (the CLI root
  span covers the command; precise `bss-orchestrator`-service attribution for
  `bss trace for-ask` is deferred with the broader tracing-parity pass).

**s18a — move cockpit `bubble` + `guards` into `bss-cockpit` (`60e6afa`).** The REPL's
turn driver needs the same assistant-bubble finalization + destructive/recap guards the
browser cockpit uses; they lived in `portals/csr/src/{bubble,guards}.rs`, CSR-private.
Relocated both into the shared `bss-cockpit` crate (mirrors s17a). No cycle —
`bss-cockpit` can't depend on `bss-orchestrator`, and `guards` carries its own
cockpit-specific `is_destructive` (the 11-prefix list, NOT safety's 33). The one
cross-crate guard test moved to `portals/csr/tests/cockpit_guards.rs`. Pure relocation,
workspace green. **The REPL's foundations are now all shared: `build_agent_registry`,
`astream_once`, `finalize_bubble`/`BubbleCtx`/`DestructiveCall`/`is_destructive`,
`ConversationStore`, `build_cockpit_prompt`, `renderers::dispatch::render_tool_result`.**

**s18b — reedline REPL scaffold + turn driver (`<pending>`).** `bss` with no
subcommand now launches the cockpit REPL (`cli/src/repl.rs`, `main.rs` `None` arm).
Adds `reedline` (0.38) + `indexmap` to the CLI. `bootstrap()` requires `BSS_DB_URL`,
validates `BSS_REPL_LLM_AUTONOMY` fail-closed (`read_autonomy_mode`), builds the
`ConversationStore`, and reads the model label from `bss_cockpit::current(None)`
(falling back to the orchestrator default); `actor = OPERATOR_ACTOR`. The initial
conversation resumes the operator's most-recent active session (`list_for`) or opens
a fresh one. The registry (`build_agent_registry`) + `OpenRouterChatModel` are built
once and reused. The **turn driver** mirrors the CSR `run_turn` sink to the terminal:
prior transcript before `append_user_turn`, `consume_pending_destructive` →
allow-this-turn, `build_cockpit_prompt`, `astream_once` over the
`operator_cockpit`-filtered registry inside a `bss_context::scope` with
`actor=OPERATOR_ACTOR` / `channel="cli"` / `service_identity="operator_cockpit"` (the
REPL attributes to the human operator, NOT `channel="llm"` — CLAUDE.md v0.5); collect
captured/last_proposal/executed/tool_rows, render cards via `render_tool_result`,
`FinalMessage` → `strip_reasoning_leakage(strip_channel_markup(...))`; then the shared
`finalize_bubble` for the text + warnings; `append_assistant_turn`;
`set_pending_destructive` + "Pending /confirm for …" on a staged proposal; the `bss ai`
prose panel unless a card already answered. Slash: `/help /confirm /exit /quit` (the
rest are stubbed with a pointer). `/confirm` drives the synthetic-confirm turn.
**Verified:** boots, connects the store, resumes the session, builds the registry,
renders the banner, exits cleanly; the turn driver reuses the live-verified `bss ask`
path (`astream_once` + `finalize_bubble`). Interactive input needs a real TTY (reedline
requirement) — the operator tests turns in a terminal; a piped/`script` PTY can't
satisfy reedline's cursor-position query.

**Remaining P7 — the big pieces:**
- **The reedline REPL — remaining slices** (`cli/bss_cli/repl.py`, 1301 lines):
  - **s18c — session slash commands:** `/sessions /new /switch /reset /focus` (the
    `/help /confirm /exit /quit` core landed in s18b).
  - **s18d — `/360 /ports /config /operator` + the `_maybe_intent_match` list-intent
    intercept** (deterministic tool dispatch that skips the LLM for clean "list X" /
    "show X" prompts — `_drive_intent_turn`).
- The **scenario engine** (ports against recorded Python-runner runs) + **onboard** (666,
  the compound signup flow) + **bss-seed** / **bss-admin** CLI wiring.
- **Cockpit knowledge wiring** (small follow-up): rebuild the cockpit registry in
  `build_state_with_db` with `knowledge_pool = Some(pool)` when `BSS_KNOWLEDGE_ENABLED`,
  so the browser cockpit gains `knowledge.*` too (currently CLI-only).

---

## Phase 6 — portals (self-serve, csr) — ✅ CODE COMPLETE (acceptance deferred)

The first phase to **cut over deployable containers again** since P4 — the two
portals (self-serve 9001, cockpit/csr 9002) that link the P5 library crates
(orchestrator, cockpit-core, knowledge). Decomposition (03-PHASES §Phase 6):
**P6a** the shared crates (`bss-branding`, `bss-portal-auth`, `bss-portal-ui`,
`bss-webhooks`), **P6b** self-serve (~65 endpoints), **P6c** csr/cockpit + CRM
screens. Exit: `make e2e` green vs the Rust portals + the 4 standing hero
failures closed (→ 19/19).

**⚠️ Acceptance note — the "branding text" hero failure is not a bug, it's a
stale assertion.** `scenarios/portal_self_serve_signup_direct.yaml` step *visit
/welcome* asserts `body_contains: ["bss-cli self-serve", …]`. Post-v1.8 the
`/welcome` template renders `{{ branding().brand_name }} self-serve`, and the
**brand name is operator-configurable** (`[branding]` in `settings.toml` +
`BSS_BRAND_*` env). The tech-vm stack runs a **custom** operator brand, so the
hardcoded `"bss-cli self-serve"` no longer matches — and fails **identically on
Python and Rust** (why it was logged "not a regression"). The P6 acceptance fix
is to make that assertion **brand-aware** (assert the configured `brand_name`, or
the structural `"self-serve"`/`"Sign in"`/`"Browse plans"` parts), not to change
portal behaviour. Tracked as the branding half of the P6 acceptance task.

### Phase 6c slices 1–2 — cockpit skeleton + the ASCII renderers — 🚧 IN PROGRESS (2026-07-15)

**s1 — skeleton + the two foundations.** The `portals/csr` crate: config (no auth
settings — v0.13 retired the CSR login), branding-aware MiniJinja templating
reusing the existing Jinja templates via the same two-dir ChoiceLoader as
self-serve, the static mounts, `/health`, and the **`operator_cockpit` named-token**
client bundle (so `audit.domain_event.service_identity` distinguishes cockpit
traffic). No inbound token middleware, by doctrine (DECISIONS 2026-05-01). Plus:

- **`views.rs`** — the CLAUDE.md rule *"read payload keys through `field`"* made
  concrete: BSS mixes TMF camelCase with snake_case DTOs, and hardcoding one family
  blanks fields silently (the v0.13 case page did exactly that). Golden-checked;
  the subtle cases all matter — `False`/`0` are **real values** while `None`/`""`
  are skipped, `fmt_dt` degrades a non-ISO string to its first 16 chars rather than
  erroring, `flatten_case`'s ticket count prefers the id list *unless empty*.
- **`bss-orchestrator::autonomy`** — `read_autonomy_mode()` was **not ported in
  P5c**. It's a doctrine-guarded single seam and the cockpit boot is its consumer,
  so it lands here. Fail-closed tested: a typo refuses the boot; `"true"` is not a
  sneaky alias for `batched`.
- **Boot-order papercut fixed** (noted in P6b s5): the cockpit inits telemetry
  **before** state construction, matching Python's lifespan, so store/client boot
  warnings are visible rather than emitted into a void. **self-serve's `main.rs`
  still has the old order — an owed follow-up.**

**s2 (a–d) — the ASCII renderers**, paying down the P5b debt. All **byte-golden**
against the oracle (fixtures captured from the *live* Python renderers; the ASCII
is fed to the LLM as well as the operator, so one shifted column is a real
regression). **29 golden cases green.**

- **s2a `boxes`** — `state_dot` / `progress_bar` / `box` / `double_box` /
  `format_msisdn` / `format_iccid`. The seam is **character-vs-byte width**:
  Python's `len()`/slicing/`ljust()` count *characters*, Rust's `len()` counts
  *bytes*, and every framed line carries `●`/`—`/`█`. Pinned by tests asserting
  every row is exactly `width` chars and a 200-rune line truncates on the char
  boundary. **Capturing goldens caught my hand-written expected strings being
  wrong** (mis-counted the top border) — the oracle is the authority, not a
  reading of the format string.
- **s2b `fmt` + `subscription`** — the flagship view, plus the Python format
  primitives the family depends on. Three things Rust silently gets wrong:
  **`round()` is banker's** (`round(2.5)==2`; Rust's `f64::round` gives 3 — a
  bundle on exactly `x.5%` renders a percent off); **`str.title()`** treats any
  non-alpha as a word boundary so `data_roaming` → `Data_Roaming`; padding counts
  chars. Also faithful: `not total` catches `0.0`, `timedelta.days` **floors**
  toward −∞, an unparseable date passes through raw.
- **s2c `tables`** — ticket / prov / inventory / case / port_request. case's title
  is `{subject!r:<40}` → an apostrophe flips the repr to **double** quotes (the
  same repr seam as the P6b s14d audit text); ticket's case id comes from the
  **last** matching `relatedEntity` (no `break`); port_request omits the
  rejection-reason row entirely when absent.
- **s2d `customer`** — the 360. **`_bundle_pct` truncates (`int(...)`) while the
  subscription renderer's balance rows use banker's `round()`** — two percent
  calculations, two rounding rules, one package. Reproduced rather than tidied into
  agreement (R5). Likewise `contact_line` lets a later medium of the same type
  overwrite an earlier one, deliberately unlike `bss_csr.views.flatten_customer`'s
  `if not email` guard.
- **`order`** — the SOM decomposition tree (the trickiest layout). **The RFS loop
  is nested inside the CFS loop in the oracle, so two CFS nodes would render the
  RFS list twice.** v0.1's decomposition is 1 CFS → 2 RFS so it never bites, but
  it is reproduced faithfully — "fixing" it would be a behaviour change (R5).

**s2 (e–h) — the rest of the renderer family.** `order` (the SOM decomposition
tree — the RFS loop is nested *inside* the CFS loop in the oracle, so two CFS
nodes render the RFS list twice; v0.1 is 1 CFS → 2 RFS so it never bites, and
"fixing" it would be a behaviour change — R5), `catalog` (the `%g`-vs-`str()`
split in one function; `inf`-sinking stable sort; `expiryHours: 0` is falsy → the
dash), `esim`, and `dispatch`.

**⚠️ `dispatch` is the single rendering rule.** Its 18-tool set was diffed exactly
against the Python `RENDERER_DISPATCH` keys and is now **inventory-locked** — a
tool silently dropping out downgrades to raw JSON on both surfaces with nothing
failing. One deliberate divergence: Python wraps the renderer in
`except Exception: return None`; the Rust renderers are total over `Value`, so
there is no exception to catch and a panic would be a **bug, not a fallback** —
left un-caught so it surfaces in tests.

**⚠️ RESOLVED SEAM — the eSIM ASCII QR is NOT byte-identical (human call).** See
`04-RISKS-AND-DECISIONS.md` §"Resolved seam". python-qrcode and the Rust `qrcode`
crate encode the same LPA payload into different matrices — different **mode
segmentation** *and* a different mask. Forcing the mask is not enough (proven by
driving the crate's canvas with mask 7, python's pick, and still diverging), which
locates it in the data encoding. Both are valid QRs scanning to the identical
string; both pick the same *version*, so the card's dimensions are unchanged. The
test asserts **byte equality on every non-QR line** and the QR block on its
**functional** contract only. **This is the one place in the port where
byte-identical is knowingly not the standard.**

### Phase 6c slice 3 — the cockpit routes (a–c) — 🚧 IN PROGRESS (2026-07-15)

**s3a `guards`.** The pure, rule-bound guard logic from `cockpit.py`.
**⚠️ THE FINDING: the cockpit has its OWN destructive list.**
`_DESTRUCTIVE_PREFIXES` (33 prefix entries) is **not**
`bss_orchestrator.safety.DESTRUCTIVE_TOOLS` (11 exact entries); they overlap only
partially, and CLAUDE.md names safety's as *"the destructive list"*. It isn't
drift, quite — the two have different jobs: **safety's decides what the loop
blocks; the cockpit's decides what gets staged as a `/confirm` proposal.** A
broader staging list is harmless. The direction that *would* hurt is a tool the
loop blocks but the cockpit can't stage — the operator hits
`DESTRUCTIVE_OPERATION_BLOCKED` with **no `/confirm` prompt to authorise it**.
That set is empty *only because* `admin.force_state` /
`admin.reset_operational_data` aren't in the `operator_cockpit` profile — an
invariant of the **profile**, not the code, so it is now a **test** that fires if
someone adds `admin.*` rather than stranding an operator at runtime. Also ported:
the tool-recap suppressor (a `<pre>` bubble, or **2+** canonical `Header:` lines —
one is legitimate commentary, hence the threshold) and the v0.20 citation guard.

**s3b `sessions`.** The index's bucketing/humanising/title logic, clock injected.
The buckets are **midnight-anchored, not rolling**: 23:59 yesterday is "Yesterday"
despite being <24h ago, and 8 days back is "Older" despite being inside a rolling
week. A rolling implementation passes casual testing and drifts at the edges —
both sides of every cut are pinned, as are `%b %d` zero-padding (`Apr 03`) and the
80-vs-81 truncation boundary.

**s3c `turn`.** The transcript-block parser + `plan_turn()` — where the turn's
*correctness* lives (does the LLM run, on what prompt, destructive or not),
extracted from the SSE plumbing and tested. Pinned: an **answered** user message
replays instead of re-running (page reloads are free) while a **trailing tool row
is not an answer** (interrupted turn → still drives); the **last** user block is
the prompt; and **`/confirm` is a `startswith` guard** — *"should I type /confirm
now?"* must NOT authorise, or an LLM echoing the word could self-authorise a
destructive turn. The v0.13.1 interceptor authorises even with no stashed pending
row (models leak tool-call markup as text, so nothing gets stashed while the
operator's intent stands) — one turn only; the policy layer stays the server gate.

**⚠️ Carried into the SSE slice: the cockpit turn is DETACHED (v1.6.1).** It runs
in a task that persists its results *no matter what the socket does*, and a
reconnect attaches as an **observer** (`_INFLIGHT`) instead of re-driving. This is
**semantically opposite** to the P6b self-serve chat, where a dropped receiver
*cancels* the turn — so `astream_once_to`'s `false`-means-stop sink must **not** be
pattern-matched here.

**s3d–s3g + s2i — `cockpit.py` is PORTED END TO END, and the renderer family is
COMPLETE (45 golden cases).**

- **s3d `strip_fake_propose`** — the last P5b deferral, landed with its cockpit
  consumer exactly as `chrome_filter`'s doc predicted (its narration-lead
  lookbehind is why `fancy-regex` was chosen). Byte-golden ×10. The case that
  matters: prose **legitimately** mentioning `/confirm` survives untouched AND is
  not flagged — `was_modified` reflects only the banner + call strips, never the
  /confirm-sentence strip, because that flag gates the caller's stall warning.
- **s3e `bubble`** — the override chain. **The staged pending row is the truth and
  the bubble must match it**, whatever the model wrote: all three observed
  wrap-ups after a BLOCKED destructive ("Done." implying it ran; a mimicry-shaped
  propose paired with a *real* tool_call so the stall warning doesn't fire; empty)
  collapse to the canonical `Proposed X(args). Type /confirm to authorise.`
  **⚠️ A test of mine was wrong and the oracle corrected it:** `mentions_confirm`
  is computed on the **post-strip** text, so `"Type /confirm and I'll do it."` gets
  eaten whole by the boilerplate regex and does **not** stall. That surfaced a
  **faithful oracle quirk, now pinned**: the empty-check runs *before*
  `strip_fake_propose`, so a bubble stripped to nothing stays **empty** — not
  `(no reply)`, no stall. Reproduced under R5; **the fix belongs in the Python
  first.**
- **s3f `tool_row` + `inflight`** — `&#10;` newline encoding is **not cosmetic**:
  SSE requires `data:` be one physical line, and a raw `\n` splits the frame and
  silently drops every card line after the first. The registry is the v1.6.1
  contract — **the turn is DETACHED and a reconnect OBSERVES**, the opposite of
  P6b's chat where a dropped receiver cancels; pinned by a test that drops every
  observer and asserts the turn still persists. `broadcast` (not `mpsc`) so a
  second tab observes rather than steals.
- **s3g — the routes assembled.** Thin by design; the correctness lives in
  `turn`/`bubble`/`guards`. **Live-smoked:** all 8 routes resolve, unknown session
  → 404, and **the sessions index read REAL sessions from the shared `cockpit`
  schema — the same rows the Python REPL writes**. A real OpenRouter turn streamed
  `live` → tool pill → tool-row `<pre>` → **heartbeat** (the 10s beat firing during
  LLM silence) → bubble → `done`, and all three rows persisted to
  `cockpit.message`. Registry omits `trace.*`/`knowledge.*` — they need a
  Jaeger/Audit/PgPool handle this bundle doesn't carry; they land with P7's CLI
  wiring where the registry is built once and shared.
- **s2i `trace`** — the swimlane. `tag["value"] is True` is an **identity** check
  (a truthy `1` does not mark an error); the v0.9 identity column hides entirely
  when no span is tagged, so pre-v0.9 traces stay clean.

**P6c CRM screens + settings/branding/handoff — ✅ PORTED END TO END (2026-07-18).**
All seven CRM route modules (`customers`, `cases`+`case`, `orders`, `catalog`,
`subscriptions`, `search`) plus `handoff`, `settings`, `branding` are ported. The
v1.6.1 two-step confirm is pinned in **both directions across all ten** of the
oracle's `_CONFIRM_GATED` entries in `rust/portals/csr/tests/routes_crm.rs` — a
bare POST bounces with a gate-refusal flash and never touches a client; `confirm=yes`
falls through to the policy layer. Commits `ca645d7`…`bf20585` (s4a–s4f + s5a/s5b).

**Owed client gaps closed while porting (all real — no earlier caller needed them,
not port artefacts):** `CrmClient::list_customers`/`list_cases` and
`ComClient::list_orders` had silently dropped Python's `limit`/`offset` paging →
added `*_paged` wrappers (defaults-wrapper shape). `CrmClient::transition_case` /
`update_case_priority` and `CatalogClient::list_promotions` /
`admin_retire_offering` were **entirely absent** → added. The ticket FSM trigger
maps (`_TICKET_STATE_TO_TRIGGER` + in-progress-by-source) moved from
`tools/ticket.rs` onto `CrmClient` so the cockpit workbench and the tool share one
copy (Python keeps them on the client for exactly this reason). `CockpitClients`
gained the `mediation` field the subscription usage panel needs.

**s5a — the config writers.** The five `write_*` helpers that the read-side
`config.rs` explicitly deferred to P6 now land in `bss-cockpit`
(`write_operator_md`/`write_settings_toml`/`write_branding_settings`/
`write_branding_logo`/`remove_branding_logo`). The v1.8 doctrine property is
test-pinned: a `[branding]` save preserves operator comments in every OTHER
settings.toml section (needs a `toml_edit` round-trip — new direct dep, already
transitively present). Logo writes are magic-byte sniffed (PNG/JPEG/WebP, never
SVG), 256KB byte-capped, fixed filenames, stale-sibling cleanup.

**Oracle-captured seams (pinned, not assumed):** the `quote_plus` redirect encoder
(space→`+`, `/`→`%2F`, UTF-8 per byte — NOT the self-serve `next=` encoder); the
`{trigger!r}` single-quote transition-error copy; `datetime.fromisoformat()
.isoformat()` normalisation (datetime-local gains `:00`, date-only→midnight, bad
input carries the verbatim `Invalid isoformat string: '…'`). The catalog retire
keeps its own gate copy ("Check the confirm box…"), so the confirm test accepts
either wording while still forbidding a service-error flash.

**Remaining in P6:** **P6 acceptance** (hero 19/19 + the brand-aware assertion).
**P6b's prod-only webhooks** (Resend/Didit, 412 LOC) stay deferred — not on the
hero path. `trace.*`/`knowledge.*` remain absent from the cockpit tool registry
(need Jaeger/Audit/PgPool handles the portal bundle doesn't carry; they land with
the CLI/REPL wiring in P7).

---

### Phase 6b slice 14 — chat SSE (a–e) — ✅ PORTED (2026-07-15)

**P6b self-serve is now feature-complete.** The last customer-facing feature, in
five sub-slices, each committed + gated:

**s14a — `chat_caps`** (`orchestrator/bss_orchestrator/chat_caps.py` →
`bss-orchestrator/src/chat_caps.rs`). Hourly sliding window (in-memory,
per-customer + per-ip), monthly cost cap over `audit.chat_usage`, token×rate cost
accounting. Two deliberate port shapes:
- The cap **decision** is factored out of the IO as a pure `decide()`, so the
  rules (hourly-first ordering, month/year rollover `retry_at`) unit-test with no
  database — same shape as `build_attest_body` (s6).
- The **pool is injected**, not lazily self-created. Python builds its own
  `AsyncEngine` because the orchestrator library has no handle on the portal's
  engine; in-process in Rust the portal already owns a `PgPool` against the same
  Postgres. Same DB, same SQL, same semantics.
- Fail-closed preserved + tested (no pool / DB error → `cap_check_failed`, never
  allowed). `record_ip_request` ported for parity though it has **no caller in the
  oracle either** — vestigial, documented (cf. the `tok_FAIL_` branches, s7).
- Cost math golden-checked against the running oracle: **65 / 100 / 0 / 1 / 1000**
  cents + period `202604`, including both warning event names.

**s14b — `astream_once_to`.** P5c left `astream_once` as collect-to-`Vec` with a
note that "a true streaming variant lands with the SSE portal wiring in P6". The
loop now emits through a `&mut (dyn FnMut(AgentEvent) -> bool + Send)` sink;
`astream_once` is a thin collecting wrapper, so **every existing caller and test is
untouched**. The sink's `bool` is the Rust shape of Python's `GeneratorExit`.
*Worth recording:* the three points where the Python **consumer** returns early
(ownership violation, error, final message) are all **already terminal in the loop
itself**, so the sink form emits exactly the collecting form's sequence — no tool
call can execute "past" an early return. Pinned both ways by test.

**s14c — conversation + turn stores** (`chat_session.py`). Python hands out the
live object and the SSE handler mutates it in place (`conv.append`, `turn.done =
True`) → `Arc<Mutex<..>>` values, so the handler gets the same aliased state, not a
copy. **`transcript_text()` is a frozen contract** (SHA-256'd into
`crm.case.chat_transcript_hash`): golden-checked three ways against the oracle and
**pinned by digest** (`cad2a20c…57a2`) so the join/trailing-newline rules can't
drift silently.

**s14d — the ownership trip-wire, finally wired.** Closes the P5c deferral. Until
now `assert_owned_output` was exported but **never called** — the Rust customer
chat would have had **no output-ownership enforcement at all**, a security
regression vs the oracle. Now wired exactly where Python has it (after the stuck
bail; gated on actor-bound && `!is_error` && name). Needed `record_violation`
(best-effort CRM interaction → server-side `audit.domain_event`) and an **owed
client fix**: `log_interaction_full` — the 3-arg port hardcoded
`direction="inbound"` because its one caller wanted the defaults, and
`record_violation` needs `outbound`. Same pattern as `attest_kyc` (s6): the 3-arg
form is now a defaults wrapper, zero churn.
- **Golden-checking caught a real bug:** Python's `{tool_name!r}` is a repr →
  **single** quotes; Rust's `{:?}` emits **double** quotes, which would have
  silently drifted the permanent audit text. Fixed via `py_repr_str`.
- Pinned: summary/body wording, `py_repr` across 7 value shapes, the interaction
  wire body incl. key order (D9), and **char-wise** (not byte-wise) transcript
  truncation — Python's `s[:1000]` counts characters and a byte slice would panic
  mid-codepoint.

**s14e — the routes.** `/chat`, `/chat/widget`, `POST /chat/message`, `POST
/chat/reset`, `GET /chat/events/:session_id`. `AppState` gains
`chat_conversations` / `chat_turns` / `chat_caps` / `chat_registry`. The registry
is the chat agent's **own** client bundle (3 public catalog reads + the `*.mine`
wrappers), separate from `PortalClients` — mirroring Python, where the orchestrator
holds its own `get_clients()`. New `BSS_MEDIATION_URL` (`usage.history_mine`).
SSE streams the pre-encoded `bss_portal_ui` frames as **raw bytes** rather than
through axum's `Sse` type, which would double-encode what `format_frame` already
built. The v0.13.1 escalation-hallucination detector verified against the **real**
oracle regex (all 9 first-person-active claims trip; all 5 past-tense/third-person
recaps don't — a customer asking "what's my case ID" must not be false-positived).

**Live-smoked** against the tech-vm stack (the axum-0.7 `:param` lesson from s9 —
unit tests cannot see route registration):
- all 5 routes registered; unauth → 303 login, **not** 404
- unknown session → 404; cross-customer → 403 + the warn log, and the SSE host
  correctly **absent** from the non-owner's page render
- cap check hit the real `audit.chat_usage` and allowed (no fail-closed trip)
- **a real OpenRouter turn streamed live** → `live` → tool pill → bubble → `done`,
  proving progressive streaming (not batched-at-end)
- cost accounting wrote a real `audit.chat_usage` row that **sits alongside rows
  the Python portal wrote on 2026-07-12** — same table, same shape
- the **v1.5.1 fallback-rate path fired live**: the deployed model
  (`gemma-4-31b-it`) is in neither the rate table nor the configured-model
  fallback, so it took the conservative ceiling. The Python oracle does the
  identical thing — an observation about the stack's config, **not a port bug**,
  and not ours to "fix" under the behaviour freeze (R5).

Tool calls failed in the smoke because BSS services are docker-internal
(`http://crm:8000`) and not host-exposed from this dev box — the known limitation;
they land in the P6 hero-suite acceptance. The graceful degradation
(`chat.prompt_context_load_failed` → `(loading)` placeholders, turn continues) is
itself correct behaviour and was observed working.

**Verified:** workspace `clippy -D warnings` clean; **111 test groups green**.

**P6b self-serve status.** The entire portal is ported (s1–s14). The only
remaining piece is the **prod-only webhooks** (`/webhooks/resend`,
`/webhooks/didit`) — Resend + Didit are deferred throughout this port (sandbox runs
logging-email + prebaked-KYC, so they are never hit, and they're not on the hero
path). Signature verification is ready in `bss-webhooks` (P6a); they land with
their DB stores when the prod providers do. **Next: P6c (cockpit) + P6 acceptance.**

---

### Phase 6b slice 13 — session-status JSON API + P6b remaining-work note — ✅ PORTED (2026-07-14)

`GET /api/session/:session_id` — the read-only JSON projection of the in-memory
signup session that the **scenario runner's HTTP step** polls (`done` + the
resulting ids). Public (no session), matching the Python route.

**P6b self-serve status (as of this slice).** The entire customer-facing **account
+ signup surface** is ported and route-smoked (s1–s13): public pages, auth/login,
step-up, signup funnel (create→KYC→COF→order→poll→confirmation), dashboard, profile
(+cross-schema email change), payment-methods, plan-change, cancel, top-up,
billing, eSIM, msisdn picker, activation, session-status API. Two pieces remained
at s13: **chat SSE** (→ landed in s14) and the **prod-only webhooks** (still
outstanding; see the s14 entry above for the current status).

---

### Phase 6b slice 12 — subscription writes + billing/esim — ✅ PORTED (2026-07-14)

The rest of the account surface: plan change, cancel, top-up (step-up writes) +
billing history & eSIM pages (reads).

- **`account_reads.rs`** — `GET /billing/history` (paginated `list_payments` +
  `count_payments` + method last-4 index + purpose labels) and `GET
  /esim/:subscription_id` (ownership-checked LPA code + PNG QR). Both unit-tested
  helpers.
- **`account_writes.rs`** — plan change (`GET/POST /plan/change`, `/plan/change/
  cancel`, `/plan/change/scheduled`) with `format_price` + card builder; cancel
  (`GET/POST /subscription/:id/cancel`, `/cancelled`) with the "what's lost"
  panel; top-up (`GET/POST /top-up`, `/top-up/success`). All writes step-up-gated
  + ownership-checked (not-found == not-yours), one `bss-clients` write each,
  `portal_action` on success + failure.
- **clients**: `payment.count_payments` + `list_payments` offset passthrough (3
  callers pass `0`).

**Verified:** clippy + 111 workspace groups green; billing/purpose-label + last-4
unit tests; all 10 routes smoke-gate on the binary (→ 303 login).

---

### Phase 6b slice 11 — payment methods (list/add/remove/set-default) — ✅ PORTED (2026-07-14)

The card-on-file management surface (mock mode). `payment_methods.rs`: `GET
/payment-methods` (list), `GET/POST /payment-methods/add` (mock card form →
server-side tokenize → `create_payment_method`), `POST /payment-methods/:pm_id/
{remove,set-default}` — all step-up-gated with an ownership check. Reuses the
signup tokenizer (`local_tokenize`, now `pub(crate)`) and the profile
sensitive-write helpers (`parse_form`/`field`/`user_agent`/`audit`, now
`pub(crate)`).

- **clients**: `payment.create_payment_method` gains `exp_month`/`exp_year`
  passthrough (signup + orchestrator callers pass `12, 2030` — their prior
  defaults); `payment.set_default_method` ported.

**Deferred:** the Stripe Checkout add flow (`add/checkout-init` + `checkout-return`,
prod-only — sandbox runs mock; the `add` route bounces stripe-mode there).

**Verified:** clippy + 111 workspace groups green; all routes smoke-gate on the
binary (→ 303 login).

---

### Phase 6b slice 10 — profile (contact details + cross-schema email change) — ✅ PORTED (2026-07-14)

The first step-up-gated account surface + the cross-schema email-change subsystem.

- **`bss-portal-auth` `email_change.rs`** — `start_email_change` (uniqueness
  check → void prior pending → mint OTP → send to the *new* email),
  `verify_email_change` (**the cross-schema atomic write**: OTP match → CRM
  `contact_medium.value` + `portal_auth.identity.email` + pending consumed, all
  in one sqlx transaction), `cancel_pending_email_change`. Result enums
  `StartOutcome`/`VerifyChangeOutcome`. This is the documented doctrine exception
  (DECISIONS 2026-04-27).
- **`profile.rs`** — `GET /profile/contact` + name/phone/address updates (step-up
  `name_update`/`phone_update`/`address_update`, ownership+type check for
  phone/address) + email change (`.../email/change` step-up-gated, `.../email/verify`
  where the OTP *is* the step-up, `.../email/cancel` ungated). One `bss-clients`
  write per route; `portal_action` on success + failure.
- **clients**: `crm.list_contact_mediums`, `update_individual`,
  `update_contact_medium` (PATCH) ported.
- **`stepup.rs`**: `check_step_up` finalised — computes the safe same-origin
  Referer (`safe_referer_path`) internally for the bounce `next`.

**Verified:** clippy + 111 workspace groups green; profile routes smoke on the
binary (gated → 303 login with a proper form body). The email-change atomic
commit is exercised in the P6 hero acceptance (needs CRM/party/contact_medium
fixtures on the live stack).

**Note:** `RawForm` needs the `application/x-www-form-urlencoded` content-type
(browsers always send it). A content-typeless POST 415s at the extractor before
the gate — immaterial to real traffic; noted.

---

### Phase 6b slice 9 — dashboard + eSIM QR + picker/confirmation/activation — ✅ PORTED (2026-07-14)

The read-y post-login surface: the customer **dashboard** (`/`), the MSISDN
**picker**, and the post-signup **confirmation** (eSIM QR) + **activation** pages.

- **`dashboard.rs`** — `subscription.list_for_customer` + per-line `get_balance`
  + `catalog.list_offerings` (names) + `list_customer_offers`. Ports `_bar_for`
  (proportional fill, low/exhausted/unlimited), `_days_remaining`, `_cta_for`,
  `_line_view` (roaming-0 filter, applied-promo badge), and `discount_label`
  (`20% off` / `SGD 5.00 off`) — all unit-tested. Empty-state for unlinked /
  no-subscription identities.
- **`qrpng.rs`** — real PNG QR via new workspace deps `qrcode` + `image`
  (encode from the module matrix; dark `#0e1014` on white, box 8, border 2).
  Byte-for-byte parity with Python's `qrcode` lib is not a wire contract; the
  payload/layout/colours match. PNG-magic test.
- **signup.rs routes** — `GET /signup/:plan_id/msisdn` (available-number picker),
  `GET /confirmation/:subscription_id` (QR + activation code w/ inventory
  fallback + the completed step timeline), `GET /activation/:order_id` (+ `/status`
  poll fragment → `HX-Redirect` to confirmation).
- **clients**: `catalog.list_customer_offers` ported.

**Verified:** dashboard math + `discount_label` + QR PNG unit-tested; all five
routes smoke-gated on the binary (→ 303 login). Full data render needs the
subscription/inventory services (not host-exposed) → P6 acceptance.

**Note — middleware vs deps gating:** Python's `PortalSessionMiddleware` gates
every non-allowlisted route; the Rust `session_layer` only *resolves* the cookie,
so each route self-gates via `deps::require_*`. `/confirmation` + `/activation`
therefore take `require_session` explicitly (Python relied on the middleware,
having no route-level dep). Behaviour matches; the enforcement seam differs.

---

### Phase 6b slice 8 — step-up auth (OTP grant + pending-action replay) — ✅ PORTED (2026-07-14)

The **sensitive-write gate** — prerequisite for every account-surface write
(profile / payment-methods / plan-change / cancel / top-up). Closes the last
deferred piece of `bss-portal-auth`.

- **`bss-portal-auth` step-up flow** (service.rs): `start_step_up` (rate-limited
  per session, mints a `step_up` OTP scoped to `action_label`), `verify_step_up`
  (timing-safe match → consume OTP → mint a one-shot `step_up_grant`), and
  `consume_step_up_token` (atomic one-shot consume at the write). `StepUpError` /
  `StepUpVerify`.
- **`pending_action.rs`** — `stash_pending_action` / `consume_pending_action`
  over `step_up_pending_action` (JSONB payload, partial-unique supersede,
  `step_up_token` stripped). The POST-body stash that makes the bounce→verify→
  replay seamless.
- **portal `/auth/step-up` routes + `check_step_up` gate** (`stepup.rs`): GET
  form, POST `/start` (issue OTP), POST verify (→ grant cookie + replay page or
  303). `check_step_up` reads the grant from header→form→cookie, consumes it,
  and on miss stashes + bounces to `/auth/step-up`. `require_session` added to
  `deps`.

**Live-validated:** `stepup_live` round-trip vs the real `portal_auth` schema —
start → wrong-code `Failed` → correct-code grant → wrong-`action_label` reject →
one-shot consume (second = false) → pending stash/consume with `step_up_token`
filtered. Route smoke on the binary (GET form 200; unauth POSTs → 303 login).

**Unblocks:** the account-surface slices wire `check_step_up(action_label)` into
each sensitive write; every label is already in `SENSITIVE_ACTION_LABELS`.

---

### Phase 6b slice 7 — signup funnel part 2b (COF mock + order + poll) — ✅ PORTED (2026-07-14)

Finishes the **deterministic sandbox happy path** — a customer can now sign up
end-to-end (create → KYC → card → order → activate) with zero LLM round-trips.

- **`bss-clients` `com.create_order` += `skip_assigned_offer`** (sends
  `skipAssignedOffer: true` only when set). One existing caller (orchestrator
  `order.create`) updated to pass `false` — matches the Python tool, which
  doesn't expose it.
- **`POST /signup/step/cof`** (mock tokenizer path) + `signup_step_cof_mock` +
  `local_tokenize` (brand/last4 + `FAIL`/`DECLINE` token markers — the marker
  branches are **vestigial**, as the numeric-only guard rejects letter-bearing
  PANs first, faithfully preserved). Tokenize → `payment.create_payment_method`
  (sandbox) → clear `card_pan` → `pending_order`. Stripe Checkout is deferred
  (sandbox runs `mock`).
- **`POST /signup/step/order`** — `create_order` + `submit_order` as one
  conceptual write; missing-id → `signup.create_order.no_id`; → `pending_activation`.
- **`GET /signup/step/poll`** — `com.get_order` until `state == completed`, then
  `extract_subscription_id`/`extract_activation_code`, the two-tick
  `redirect_armed` celebration dwell, and the `HX-Redirect` to `/confirmation`.
  The `targetSubscriptionId`-not-yet-stamped race is treated as in-progress
  (retrigger), matching the oracle.

**Verified:** tokenizer (brand/last4/prefix/reject) + the sub-id/activation
extractors are unit-tested; route registration smoked on the running binary
(cof/order POST + poll GET → 303 gate; wrong method → 405). The write round-trips
need the payment/COM services (not host-exposed) → exercised in the P6 hero-suite
acceptance.

**Deferred:** the Stripe-checkout COF variant (`checkout-init`/`checkout-return`)
and the Didit hosted-UI KYC handoff — both prod-only. That leaves the account
surface (profile / payment-methods / plan-change / cancel / top-up / billing /
esim / activation / confirmation) and the SSE chat route for the remaining P6b
slices.

---

### Phase 6b slice 6 — signup funnel part 2a (KYC step, prebaked) — ✅ PORTED (2026-07-14)

The **KYC step** of the funnel + the wire-contract-critical `attest_kyc` fidelity
work it forced. COF/order/poll finish the chain in s7.

- **`bss-clients` `crm.attest_kyc` fidelity (R5 owed-fix).** The Rust client only
  had the 3-arg stub path; the Python `attest_kyc` is one method with a full
  optional param set (scenario callers use defaults, signup fills them all).
  Extended to `attest_kyc_full(customer_id, provider, token, AttestKycOpts)` with
  the 3-arg `attest_kyc` now a defaults wrapper — **no churn for the one existing
  caller** (orchestrator's `customer.attest_kyc`). Body assembly extracted to a
  pure `build_attest_body` and **golden-pinned against the Python oracle** for
  both cases (full signup body + 3-arg stub body; `verified_at` stripped as it's
  `now()` in both). This is the byte-for-byte wire body the CRM service receives.
- **prebaked KYC adapter (`kyc.rs`).** `PrebakedKycAdapter` (`initiate` →
  loopback session, `fetch_attestation` → deterministic per-email attestation),
  `KycAttestation`/`KycSession` value types, `KycAdapter` enum + `from_provider`
  (Didit falls back to prebaked with a warning until its routes land). The
  email→NRIC-stub→SHA-256 hash is **golden-pinned to the oracle** (3 emails:
  last4 + full hash + session id).
- **`POST /signup/step/kyc`** (prebaked synchronous path) + `_complete_kyc_attest`
  + the shared step helpers `resolve` (owning-identity 404 guard) and
  `render_step_fragment` (`partials/signup_progress.html`). Advances
  `pending_kyc → pending_cof`; policy violations → `failed` + audit row.

**Live-validated:** the deterministic logic is golden-pinned (adapter hash + full
attest body, both vs the Python oracle); route registration smoked on the running
binary (`POST /signup/step/kyc` unauth → 303 to `/auth/login`; `GET` → 405,
confirming POST-only). The attest round-trip itself needs the CRM service (not
host-exposed) → exercised in the P6 hero-suite acceptance.

**Deferred to s7 / the Didit slice:** COF step (mock tokenizer + Stripe checkout),
order step, poll step, `_extract_*`; and the Didit hosted-UI handoff
(`pending_kyc_handoff` + QR + `/signup/step/kyc/poll` + `/callback`, cap-exhausted
+ corroboration-timeout paths). `AttestKycOpts` carries the `document_number` /
`nationality` overrides those need.

---

### Phase 6b slice 5 — signup funnel part 1 (create-customer + form + progress) — ✅ PORTED (2026-07-14)

The signup **entry surface** — everything up to the HTMX step timeline. The
KYC/COF/order/poll step routes (+ the Stripe-checkout and Didit-handoff variants)
are the next slice.

- **shared-crate additions:** `catalog.preview_promo` (bss-clients),
  `offerings::find_plan` (portal), and two `bss-portal-auth` DB writes ported +
  exported: `link_to_customer` (idempotent 1:1 identity→customer bind, `LinkError`
  {UnknownIdentity, AlreadyLinked{existing}, Db}) and `record_portal_action`
  (`portal_action` audit row via `PortalActionRecord`; `ts` from `bss_clock::now`,
  `tenant_id` server-default like the session/login-attempt inserts).
- **new portal modules:** `error_messages` (the rule→customer-copy map + `render`
  /`is_known`), `prompts` (KYC prebaked constants), `signup_session`
  (`SignupSession` + `SignupStep` enum serialising to the Python `Literal`
  strings + TTL-bounded in-memory `SessionStore`), `deps`
  (`require_verified_email`/`require_linked_customer` — the imperative form of the
  FastAPI gates, returning a 303-to-login `Response` on the Err path).
- **routes:** `GET /signup/:plan_id` (form — plan lookup + returning-customer
  prefill/needs-card/assigned-offer best-effort reads), `GET
  /signup/promo/preview` (live promo preview), `POST /signup` (step 1:
  `crm.create_customer` + atomic `link_to_customer`, with the returning-customer
  resume-at-first-incomplete-step branch), `GET /signup/:plan_id/progress` (the
  timeline host). One `portal_action` row per write (success + failure);
  structured policy violations render via `error_messages`.

**Live-validated (as far as the env allows):** a DB round-trip smoke
(`audit_link_live`: link → idempotent re-link → `AlreadyLinked` → `UnknownIdentity`
→ audit-row write + count) against the real `portal_auth` schema; and the running
binary — login → session cookie → **authenticated `GET /signup/PLAN_M` passes the
gate and reaches `catalog.list_offerings`** (502 only because BSS services aren't
host-exposed from the dev box), unauth variants 303 to `/auth/login` with the
right `next`, promo-preview empty-code → 200 empty body. The catalog-backed form
HTML + the create-customer write land in the P6 hero-suite acceptance (full stack).

**Bug caught by the live smoke:** the `:plan_id` routes were first written with
axum-0.8 `{plan_id}` syntax; on axum 0.7 (this workspace) that is a *literal*
segment, so `/signup/PLAN_M` 404'd — the whole funnel was dark. Fixed to `:param`;
re-smoked green. Unit tests alone would not have caught the route-registration
syntax.

**Port-vs-oracle notes:** Python reads `app.state.payment_stripe_publishable_key`
which `main.py` never sets → always `""`; the Rust progress render passes `""` to
match (Checkout-redirect mode needs no client key). `main` boot-warns (db-connect
/ email-adapter failures during `build_state_with_db`) are emitted before
`init_telemetry` runs → **swallowed**; noted as a follow-up (reorder telemetry
init ahead of state build). Client IP still `None` (per-IP rate limiting inert),
carried from slice 4.

**Deferred to slice 6:** KYC step (prebaked synchronous + Didit handoff/poll/
callback), COF step (mock tokenizer + Stripe checkout init/return), order step,
poll step, and their helpers (`_local_tokenize`, `_extract_subscription_id`/
`_extract_activation_code`, `_render_step_fragment`).

---

### Phase 6b slice 4 — auth/login flow (OTP + magic-link) — ✅ PORTED (2026-07-14)

The customer login gateway, **working live through the Rust binary**.

- **`bss-portal-auth` (DB write flow):** `start_email_login` (reuse/create
  identity → mint OTP + magic-link → store both HMAC-hashed → record attempt →
  hand plaintext to the adapter, one tx) + `verify_email_login` (timing-safe
  verify → consume matched token → **auto-link to a CRM customer by unique email
  contact-medium** → stamp `email_verified_at`/status/`last_login_at` → mint
  session). Rate limits over `login_attempt` window counts; `LoginError` +
  `VerifyOutcome`; structured `LoginFailed` reasons.
- **email adapters:** `EmailAdapter` trait + `LoggingEmailAdapter` (the greppable
  dev mailbox the hero scenarios `tail`, branded subject) + `NoopEmailAdapter`
  (tests) + `select_adapter`/`resolve_provider_name`. Resend/SMTP fail-fast
  (not yet ported).
- **portal auth routes:** GET/POST `/auth/login`, GET/POST `/auth/check-email`,
  GET `/auth/verify` (magic link), POST `/auth/logout`. Generic customer-facing
  copy; `Set-Cookie` via `build_session_cookie`; email `%40`-encoded (Gmail
  +addressing). `main` fail-fasts on the pepper.

**Live-validated end-to-end (two ways):** a DB round-trip smoke (start → read OTP
→ wrong-code `Failed` → correct-code `Session` → `current_session` resolves →
consumed-OTP `wrong_code`) against the real `portal_auth` schema; and the running
binary (`GET /auth/login` 200 → `POST` 303 to check-email → OTP in the mailbox →
`POST /auth/check-email` 303 to `/plans` + `Set-Cookie`).

**Port-vs-oracle notes:** the `_mask_email` docstring says `a***` but the code
produces `a**` (`max(len-1,1)`) — the Rust matches the code. Client IP is
currently `None` (axum `ConnectInfo` not yet wired) so per-IP rate limiting is
inert; per-email limiting is active. Both noted as follow-ups.

**Deferred:** step-up (start/verify/consume + pending-action replay), the Resend
HTML adapter.

---

### Phase 6b slices 2–3 — /plans + session infrastructure — ✅ PORTED (2026-07-14)

**Slice 2 — `/plans` + clients + offerings.** `offerings::flatten_offerings`
(TMF-productOffering → template dicts: sort cheapest-first, GB/unlimited
formatting via a Python-`%g` port, voice_minutes fallback, roaming suppression)
— byte-parity gated by `offerings_golden.json` from the oracle. `PortalClients`
bundle (7 clients via `NamedTokenAuthProvider`, inventory on the CRM base URL),
best-effort in `AppState` (`None` without a token → empty view). `/plans` route
live-smoked against the tech-vm catalog (renders real plan cards).

**Slice 3 — session middleware + DB session layer + security.** The meaty infra:
- **`bss-portal-auth::service` (DB):** `current_session` / `rotate_if_due` /
  `revoke_session` over the `portal_auth` schema (sqlx runtime queries; cookie =
  session row id; `bss_clock::now()`; rotation past TTL/2 = new id + revoke old
  in one tx). **Live-smoked** against the real `portal_auth.session`/`identity`
  tables (schema-valid).
- **portal `middleware`:** `PortalSessionMiddleware` as an axum
  `from_fn_with_state` layer — resolves the cookie → `PortalSession` extension
  (anon on miss), rotates + `Set-Cookie` past TTL/2. Cookie builders match the
  Python attrs. `AppState` gains `db: Option<PgPool>`.
- **portal `security`:** the public allowlist + `is_public_path` +
  `safe_next_path` (open-redirect defence, unit-tested) + the sensitive/signup
  action-label catalogues.
- Public handlers now read `PortalSession` so the header nav reflects sign-in.

Deferred to the **auth slice (4):** the login write flow (`start_email_login`/
`verify_email_login`), step-up consume, and the email adapters.

---

### Phase 6b slice 1 — self-serve portal skeleton + public surface — ✅ PORTED (2026-07-14)

`rust/portals/self-serve` (new `portals/*` workspace member) — the **first
deployable portal container** of Phase 6. The axum app skeleton + the render
stack, proven on the public static surface (no BSS read, no session):
`/health`, `/welcome`, `/terms`, `/privacy`, `/branding/logo`, and the `/static`
+ `/portal-ui/static` mounts.

**Architectural decision — reuse the Jinja templates via MiniJinja.** The
existing `.html` templates are Jinja-compatible and MiniJinja renders them
unchanged, so the Rust portal loads them **in place** via a two-directory loader
(the portal's `templates/` then `bss_portal_ui`'s shared `templates/` — the
Python `ChoiceLoader` equivalent). No template rewrite, single source of truth
during the bilingual period, trivial parity. Branding globals are **functions**
(`branding()` / `branding_style()`) evaluated per render, so a `settings.toml`
theme/brand change hot-reloads on the next request; `bss_release` + `asset_v`
are added-globals. `base.html`'s `{% set %}`/`{% block %}`/`{% if %}`/`is
defined`/`{% include %}` all render under MiniJinja untouched.

**The branding-hero lesson, validated live.** The first test pinned the literal
`"bss-cli self-serve"` and **failed** — because the workspace `.bss-cli/settings
.toml` sets `brand_name = "Octopus"`, so the portal correctly renders `"Octopus
self-serve"`. This is exactly the stale-assertion the P6 acceptance task tracks:
the fix is a **brand-aware** assertion (`bss_branding::current().brand_name`),
not a hardcoded string. The binary boots and serves the operator brand end-to-end
(`GET /welcome` → `<title>Welcome · Octopus self-serve</title>`), confirming the
whole reused-template + branding integration works.

**Ported this slice:** `config` (portal `Settings::from_env` — full field set
carried for later slices), `templating` (the MiniJinja env + two-dir loader +
branding globals + `request_ctx`), `routes` (the 5 public handlers + the render
helper), `main` (telemetry + axum serve on 9001), `lib` (`AppState` + router +
static mounts). Brought `minijinja` (+`loader`) + `tower-http` (`fs`) into the
workspace.

**Following P6b slices:** `/plans` + landing/dashboard (first catalog read via
`bss-clients`), the `PortalSessionMiddleware` as a tower layer + the
`bss-portal-auth` DB session layer, the auth/login flow (`/auth/check-email` —
the 2nd standing hero failure), the signup + KYC funnel, the post-login account
surface, the SSE chat route (wiring `chat_caps` + ownership `record_violation`),
and inbound webhooks.

**Verification.** fmt + clippy `-D warnings` clean workspace-wide; full workspace
green (106 groups, no regression). 4 integration tests render the real templates
through MiniJinja + branding; the binary boots + serves (`/health` JSON, branded
`/welcome`).

---

### Phase 6a slice 4 — bss-webhooks (signatures + redaction + idempotency) — ✅ PORTED (2026-07-14)

`rust/crates/bss-webhooks` — the shared webhook substrate. The pure,
security-critical modules (the DB stores defer to the P6b consumer):

- **`signatures`** — HMAC-SHA-256 verification across all three schemes:
  **svix** (Resend — `whsec_<base64>` secret decode, `"{id}.{ts}.{body}"`
  signed, space-separated `v1,<b64>` rotation entries), **stripe** (`t=`/`v1=`
  comma fields, `"{ts}.{body}"` hex), **didit_hmac** (`X-Signature-V2` hex over
  the **body alone** — the timestamp binds only into the freshness check, not
  the HMAC, faithfully reproduced). Shared `check_timestamp` handles the
  seconds-vs-millis (`>1e12`) split + the `replay_window` skew; timing-safe
  compares (`subtle::ConstantTimeEq`) with the "iterate all entries" timing
  uniformity. Stable `code` on every failure (`missing_header`/`malformed_header`
  /`replay_window`/`signature_mismatch`). Built all-three-upfront per the v0.14
  doctrine (v0.16 mustn't be the first to touch shared HMAC under payment
  pressure). Brought `base64` into the workspace for the svix secret decode +
  signature encode.
- **`redaction`** — `redact_provider_payload` over `serde_json::Value`: the
  resend (mask to/from/cc), stripe (mask email/PII + card number/cvc), and
  didit (SHA-256-hash doc numbers + DOB, mask names) recursive transforms;
  unknown providers pass through (a new provider must add a rule).
- **`idempotency`** — `idempotency_key` = `"<AGG>-r<n>"` with the empty-id /
  negative-count guards.

**Byte-parity gate — the oracle golden.** `tests/golden_test.rs` replays a
fixture generated by computing **real HMACs with the oracle's formulas** at a
fixed `now`: all three schemes' valid signatures verify (and a tampered body →
`signature_mismatch`), and `redact_provider_payload` matches the oracle's
redacted JSON exactly for resend/stripe/didit/unknown. Plus unit tests for the
error-code paths (missing/malformed/replay).

**Deferred to the P6b portal consumer** (DB-backed, `integrations` schema):
`WebhookEventStore` (idempotent persist on `(provider, event_id)`) +
`ExternalCallStore` (forensic per-call log).

**Verification.** fmt + clippy `-D warnings` clean workspace-wide; full workspace
green (102 groups, no regression).

---

### Phase 6a slice 3 — bss-portal-ui (chat HTML + SSE) + bss-cockpit postprocess — ✅ PORTED (2026-07-14)

The shared **LLM-output rendering** core both portals + the REPL need. Two crates:

- **`bss_cockpit::postprocess`** (the P5b-deferred module lands here) —
  `strip_channel_markup` (Harmony/`<channel|>`/`assistantfinal` tokens),
  `strip_reasoning_leakage` (`<think>…</think>` blocks + leading/inline
  `thought` prefixes), and `knowledge_called` (the pipe-table grammar gate for
  renderer-less `knowledge.*` prose). **Uses `fancy-regex`** (new workspace dep)
  for the one lookahead (`^…thought\s+(?=\S)` — the "don't eat *thoughtful*"
  guard); the P5b note called this out exactly.
- **`bss-portal-ui`** (new crate) — `chat_html` (the customer-chat v0.12 +
  cockpit-thread v0.13 renderer: HTML-escape-first, then a whitelisted
  block+inline markdown state machine → bold/italic/code/headings/ul/ol/code-
  fence/ASCII-panel/opt-in pipe-tables; the XSS boundary) + `sse` (frame
  encoding + status dot). `chat_html`'s lookaround italics (`(?<!\*)\*…\*(?!\*)`,
  `(?<!\w)_…_(?!\w)`) also need `fancy-regex`. Depends on `bss-cockpit` for the
  strip helpers (matching the Python import).

**Byte-parity gate — the oracle golden.** A fixture of ~30 cases was captured by
feeding representative inputs through the **live Python oracle**
(`bss_cockpit.postprocess` + `bss_portal_ui.chat_html`) and pinned in
`tests/golden_test.rs`: every `strip_*` / `render_chat_markdown` /
`render_assistant_bubble` / `render_tool_pill` output matches **exactly**,
including the lookaround italics, the `"  thought   \n\nfoo" → "foo"` reasoning
strip, mixed inline markdown, code fences, ASCII panels, and opt-in vs
suppressed pipe-tables. This is the R2-style "the renderer IS a behavioural
contract" gate — a proportional-font browser divergence would be a real bug.

**Deferred to the P6b portal consumer** (land-with-first-consumer): `agent_log`
(the `AgentEvent` → widget-HTML projection — needs the orchestrator event types
+ a MiniJinja template render) and `paths`/static-asset bundling (`TEMPLATE_DIR`/
`STATIC_DIR` + `partials/*.html` + `portal_base.css` + vendored htmx). These are
app-factory-coupled.

**Verification.** fmt + clippy `-D warnings` clean workspace-wide; full workspace
green (99 groups, no regression). The oracle golden + `knowledge_called` +
`status_html`/`format_frame` units cover the surface.

---

### Phase 6a slice 2 — bss-portal-auth (security foundation) — ✅ PORTED (2026-07-14)

`rust/crates/bss-portal-auth` — **first sub-slice: the pure, security-critical
foundation** of the ~4k-LOC package. Four modules:

- **`tokens`** — the crux. OTP (6 digits via `OsRng`, the `secrets.choice`
  analogue), 32-char URL-safe magic-link/session/step-up tokens (hand-rolled
  base64url-nopad = Python's `token_urlsafe(24)`), and `hash_token` =
  **hex** HMAC-SHA-256 keyed by the pepper (Python's `.hexdigest()`, stored in a
  DB column) + timing-safe `verify_token` (`subtle::ConstantTimeEq`, the
  `hmac.compare_digest` analogue). **Golden-vector pinned:** 5 `(token, pepper)
  → hex` vectors captured from the oracle (incl. a unicode-pepper + empty-token
  case) assert byte-parity. Empty pepper → `PepperMissing` (the defensive
  `RuntimeError`, so a lifespan-wiring regression can't make every token hash
  identically).
- **`config`** — `Settings::from_env` (env prefix `BSS_PORTAL_`), the pepper +
  public URL + email-provider selection + all TTL/rate-limit scalars, defaults
  matching V0_8_0.md §1.3. Process env is the source of truth (the established
  Rust `Settings` convention), not pydantic's `.env` parse.
- **`startup`** — `validate_pepper_present` fail-fast (unset / `changeme`
  sentinel / <32 chars → the byte-matched `RuntimeError` copy), the
  bss-middleware `validate_api_token_present` pattern.
- **`types`** — the frozen public dataclasses (`IdentityView`/`SessionView`/
  `LoginChallenge`/`StepUpChallenge`/`StepUpToken`) + failure shapes
  (`LoginFailed`/`StepUpFailed`) + `RateLimitExceeded` (Display copy matched).

**Deferred to later P6a sub-slices** (DB/branding-coupled, land-with-consumer):
the `service.py` DB layer (session lifecycle, step-up, `email_change`,
`pending_action`, per-write `audit`) over the `portal_auth` schema, the
`rate_limit` window store, and `email.py` (adapters + the branding-aware HTML
renderers — they consume `bss-branding`'s palette per send).

**Verification.** fmt + clippy `-D warnings` clean; workspace green (no
regression). 9 tests (8 token units incl. the oracle golden-vector gate + a
sequential startup-validator integration test — env-mutating, so serial). Token
generators use `OsRng` (CSPRNG parity with `secrets`); the golden vectors are the
byte-parity gate the plan calls for.

---

### Phase 6a slice 1 — bss-branding — ✅ PORTED (2026-07-14)

`rust/crates/bss-branding` — the operator-branding **read path + palette
definitions** (v1.8), the crate both portals' templates + the email renderers
need. Six modules mirroring the Python package (writes stay in
`bss_cockpit.config`, unported here per the single-write-path seam):

- **`themes`** — `ThemePalette` + the 6 dark palettes as a `LazyLock<IndexMap>`
  so iteration/`picker` order matches Python's insertion-ordered dict;
  `DEFAULT_THEME_ID`.
- **`css`** — `branding_css_block` (the minified `:root{…}` var block, 16
  slots). **Doctrine pin:** a unit test asserts the exact phosphor block so a
  palette edit that diverges from the hand-written `portal_base.css` `:root`
  fallback (the no-branding render) fails in CI.
- **`marks`** — `LOGO_MARKS` + `validate_mark` (1–3 printable chars, HTML-active
  chars rejected — the email-HTML security boundary). `isprintable` parity is
  approximated as "not control, not whitespace except space" — exact for every
  tested mark; the only divergence is exotic Cf format chars a logo mark never
  carries (documented at the seam).
- **`assets`** — `sniff_image_type` (PNG/JPEG/WebP magic bytes, **never SVG**),
  `MAX_LOGO_BYTES` (256 KB), the fixed-filename allowlist (anti-traversal).
- **`config`** — `BrandingSettings` (validated) + `BrandingView` (resolved) +
  `current()`/`file_settings()`/`reset_cache()`/`branding_dir()`. Mirrors the
  P5b `bss-cockpit` config seam **exactly**: one `stat()` per call, mtime
  hot-reload, **last-good on parse/validation error**, **defaults on absence
  (never bootstraps, never crashes)**, and the `BSS_BRAND_*` env overrides
  re-read **per call** (branding is non-secret preference — deliberately unlike
  the v0.9 tokens-load-once rule).
- **`logo`** — framework-free port of `web.py` (`logo_http()` returns bytes +
  content-type + immutable cache headers as a plain struct; the P6 axum portal
  wraps it — the core crate stays web-framework-free).

**Verification.** fmt + clippy `-D warnings` clean; workspace green (no
regression). **12 tests** all pure/CI (no oracle process — the palette values
*are* the oracle): the four oracle test files ported 1:1 — `test_assets`
(sniff/cap), `test_css` (block shape + all-slots + the exact-phosphor pin),
`test_config` (defaults-on-absence, mtime reload, last-good on bad-TOML /
unknown-theme, env overrides + invalid-override-ignored, logo resolution +
degrade), plus mark validation + theme insertion order. `current()`'s
process-global cache forces the config cases into one sequential integration
test (parallel cases would race the cache + process env), same as the
`bss-cockpit` config test.

---

## Phase 5 — orchestrator lib + knowledge + cockpit-core — ✅ COMPLETE (tag `v2.0.0-phase.5`)

The hard port, and the first phase with **no deployable cutover of its own** (D3):
these are *library* crates. Their cutover happens in P6/P7 when the Rust
portals/CLI link them; until then the Python portals keep using the Python
orchestrator against the same all-Rust service plane. So the gate is **not** a
container swap + hero suite — it's **transcript parity** (fixture-driven, the
deterministic layer) + **human-reviewed live soak** (the judgment layer, R2).

**Decomposition** (sized to real acceptance gates, like P4a/b/c):

- **P5a — `bss-knowledge`** (636 Py LOC): self-contained FTS crate, reads the
  `knowledge` schema, no LLM. Golden-diffable. **The P5 pilot — done below.**
- **P5b — `bss-cockpit` core**: Conversation store + `pending_destructive` +
  chrome filter + `_COCKPIT_INVARIANTS` prompt composition + `settings.toml`
  hot-reload. Postgres-backed (`cockpit` schema); golden-diffable on transcript
  format + rows. Renderers may defer to P6/P7 (land-with-first-consumer).
- **P5c — `bss-orchestrator`**: the hand-rolled ReAct loop (LangGraph's
  `create_react_agent` becomes an explicit loop), 109 typed tools (profile by
  profile, `customer_self_serve` first), the guard stack (`wrap_destructive` +
  autonomy, 3-strike failure + identical-call bails, ownership trip-wire, chat
  caps), the `AgentEvent` stream, and the `MockChatModel` fixture player. Gate:
  fixture-corpus transcript parity. The big one.

### Phase 5c — bss-orchestrator (slices 1–16) — ✅ COMPLETE (2026-07-14)

**Slice 16 — the finale: ownership trip-wire + prompts + OpenRouter model client.**
The non-tool infra that closes P5c.
- **`ownership.rs`** — the v0.12 output trip-wire (`assert_owned_output` +
  `OWNERSHIP_PATHS` + the tiny `[*]`/`a.b` path walker + `validate_ownership_paths_
  cover_profile`). Third defence layer: every `*.mine` result whose `customerId`
  doesn't match the bound actor errors (`AgentOwnershipViolation`, boxed). The
  route-side CRM `record_violation` lands with the P6 chat route. Unit-tested
  (owned/foreign/unconfigured/empty/non-JSON/missing-key).
- **`prompts.rs`** — `SYSTEM_PROMPT` (operator ops copilot) + the two customer-chat
  templates embedded **byte-for-byte** (`include_str!`), plus `build_customer_chat_
  prompt` (placeholder fill + prior-message block) and `build_balance_summary`.
  **Doctrine guard** ported: `ITERATIVE FLOW` is present in the P5b-ported
  `COCKPIT_INVARIANTS` and **absent** from every customer-chat prompt (test-pinned).
- **`llm.rs` — `OpenRouterChatModel`** — the production `ChatModel`, a direct
  `reqwest` call to OpenRouter's OpenAI-compatible endpoint (no LangChain/LiteLLM
  hop). Temperature 0.0, `max_tokens` cap, `frequency_penalty` only when non-zero;
  messages/tools → OpenAI shape, response → `AiTurn` (content + tool_calls + usage).
  Tools carry the byte-identical description with a permissive `{"type":"object"}`
  parameter schema (strict per-tool schemars is a documented refinement — the R2 gate
  runs on `MockChatModel`, and the live soak validates real tool-calls). Unit-tested
  (request shaping + response parsing).

**🎉 End-to-end validated** (`openrouter_agent_live.rs`, `#[ignore]`): a **real
OpenRouter turn** drove the full loop against the running stack —
`OpenRouterChatModel` → `astream_once` → the model called `catalog.list_active_
offerings` → the Rust catalog service returned live plan data → the loop rendered
`• PLAN_S (Lite): 10.0 SGD/month • PLAN_M (Standard): 25.0 SGD/month …`. The entire
ported orchestrator works end to end with a live model + the live Rust services.

**Deferred to P6 (route-coupled, per the P5b precedent):** `chat_caps` (hourly +
monthly-cost, DB-backed, enforced at the chat route with per-turn cost from the model
response) and `ownership::record_violation` (CRM interaction log on trip). These land
with the portal that owns the request context.

**✅ Phase 5c COMPLETE — 110/110 tools + the ReAct loop + guards + fixture player +
ownership trip-wire + prompts + OpenRouter client. Tagged `v2.0.0-phase.5`.** The R2
gate holds: the fixture-corpus transcript parity runs green on `MockChatModel`, and
the live end-to-end turn confirms the production path.

---

**Slice 15 — the `customer_self_serve` `*.mine` wrappers (ALL 110 TOOLS PORTED).**
14 chat-surface wrappers (`tools/mine.rs`) — the v0.12 prompt-injection containment
layer. Each binds `customer_id` from `ctx.actor` (never a param) and reuses the
already-ported client methods. Machinery:
- **`ToolCtx` gained `transcript`** (threaded from `AgentConfig.transcript` in the
  loop) so `case.open_for_me` can SHA-256 + `store_chat_transcript` before opening the
  case with the escalation category/priority maps + `[category] …` description.
- **`require_actor`** → `_NoActorBound` observation when unbound (`"system"`/empty is
  the Rust analogue of Python's `actor=None` default); **`assert_subscription_owned`**
  → `_NotOwnedByActor` for cross-customer attempts (uniform shape, never leaks a
  foreign dict).
- **`annotate_pricing`** (rust_decimal) ports `_discount_label`/`_annotate_pricing`:
  `currentMonthlyCharge` = effective-or-list price, `activeDiscount` label
  (`normalize()` for percent, `{:.2}` for absolute) — unit-tested for the
  ongoing/N-renewals/singular forms.
- `usage.history_mine` fans out across the actor's lines + newest-first merge when no
  subscription is given.

**Capstone test — `both_profiles_are_fully_covered_by_the_registry`** (the
`validate_profiles` equivalent): every `OPERATOR_COCKPIT` **and** `CUSTOMER_SELF_SERVE`
tool is registered, and the chat surface equals the 17-entry customer profile exactly.

**Verification:** fmt + clippy clean; workspace green (incl. 4 mine unit tests); 14
descriptions byte-pinned. **Live smoke** (`mine_wrappers_live.rs`, `#[ignore]`) green
against tech-vm — unbound ctx → `_NoActorBound`; a bound actor reads only its own
(pricing-annotated) data; a subscription owned by a **different** customer →
`_NotOwnedByActor`.

**🎉 TOOL SURFACE COMPLETE — 110/110 tools ported.** The last slice is the
non-tool infra: OpenRouter `ChatModel` client + ownership trip-wire + chat caps +
prompts → then the R2 gate closes and `v2.0.0-phase.5` tags.

---

**Slice 14 — the last writes: promo + catalog admin + usage.simulate (OPERATOR
SURFACE COMPLETE).** Six tools. `CatalogClient` gained `create_promotion` (the
13-param create-promotion saga), `assign_promotion`, and the admin
`admin_add_offering`/`admin_add_price`/`admin_set_offering_window`; `MediationClient`
gained `submit_usage` (`roamingIndicator` only when true). Tools:
- `promo.create`/`promo.assign` (operator-visible); `catalog.add_offering`/`add_price`/
  `window_offering` + `usage.simulate` are **LLM-hidden** (scenario/CLI scaffolding) —
  pinned by `promo_catalog_admin_usage_writes_profile_and_hidden`.
- `usage.simulate`'s `event_time` defaults to whole-second `bss_clock::now()`
  (`clock_now().replace(microsecond=0).isoformat()`), matching the clock.now seam.
- `valid_from`/`valid_to` are ISO strings passed verbatim (the Python
  `fromisoformat().isoformat()` round-trip is identity for canonical values).

**Verification:** fmt + clippy clean; workspace green; 6 descriptions byte-pinned.
**Live smoke** (`promo_catalog_usage_writes_live.rs`, `#[ignore]`) green against
tech-vm — error paths only (no promotion/offering/usage row created): `multi` promo
without periods_total → policy stop, bogus promo assign → error, catalog admin on a
bogus offering → error, `usage.simulate` on an unknown MSISDN → mediation's
block-at-edge `subscription_must_exist`.

**🎉 Tool ledger: ~96/110 — the ENTIRE operator tool surface (reads + writes) is
ported.** Remaining: the **`customer_self_serve` `*.mine`** wrappers (~14, the
auth-binding/ownership slice), then the **OpenRouter model client + ownership
trip-wire + chat caps + prompts** → then `v2.0.0-phase.5`.

---

**Slice 13 — operational WRITES (inventory / port_request / provisioning).** Seven
tools. New client methods: `InventoryClient::add_msisdn_range`; `CrmClient::
create_port_request`/`approve_port_request`/`reject_port_request`;
`ProvisioningClient::resolve_task`/`retry_task`/`list_fault_injection`/
`update_fault_injection`.
- **`provisioning.set_fault_injection` is a list→find→patch composite** — reads the
  injectors, finds the `(taskType, faultType)` match, and either patches it or returns
  the `NOT_FOUND` sentinel (matching Python). Destructive (pinned).
- Port-request + provisioning writes are **operator-only** (never customer_self_serve
  — v0.17 doctrine); pinned by `operational_writes_profile_and_destructive`.

**Verification:** fmt + clippy clean; workspace green; 7 descriptions byte-pinned.
**Live smoke** (`operational_writes_live.rs`, `#[ignore]`) green against tech-vm — all
error/sentinel paths (no seed mutation): an 8-digit `add_range` prefix → `sane_prefix`
policy stop, invalid port direction → rejected before any row, bogus port/task ids →
structured errors, and `set_fault_injection` with a bogus pair → the NOT_FOUND
sentinel (exercising the list→find composite against the live injector config).

**Tool ledger:** ~90/110. Remaining: promo.create/assign, catalog admin
add_offering/add_price/window_offering (LLM-hidden), usage.simulate (LLM-hidden) —
~6 writes. Then the `*.mine` wrappers + model client + ownership/caps/prompts.

---

**Slice 12 — order + payment WRITES.** Five tools. `ComClient` gained
`create_order`/`submit_order`/`cancel_order`; `PaymentClient` gained
`create_payment_method` (sandbox path) + `remove_method` (204-empty → `{id, removed}`).
- **`order.create` is the create+submit composite** — create, read the returned `id`,
  then submit; both halves must succeed (a missing id → a `KeyError` observation).
- **`payment.add_card` runs the pure `local_tokenize_card`** — a port of the sandbox
  tokenizer (brand from the BIN, FAIL/DECLINE embedded in the token from the raw PAN,
  uuid body; invalid PAN → the single-quoted `ValueError`). **Unit-tested** for brand
  detection + the error message (uuid non-determinism kept out of the client body).
- `order.cancel` + `payment.remove_method` destructive; create/add_card/charge not —
  pinned. `payment.charge` passes the caller's decimal string verbatim (Python's
  `Decimal(amount)`→`str` is a no-op for a canonical value).

**Verification:** fmt + clippy clean; workspace green (incl. the tokenizer unit test);
5 descriptions byte-pinned. **Conservative live smoke** (`order_payment_writes_live.rs`,
`#[ignore]`) green against tech-vm — a **real** `payment.add_card` (tokenizer +
create body accepted, method created) then `remove_method` cleanup; `order.create`
with a **bogus offering** → sync structured error (no line provisioned — COF/KYC are
async, so a valid offering would reserve inventory); charge/cancel bogus-id error paths.

**Tool ledger:** ~83/110 (reads complete + CRM/subscription/order/payment writes).
Remaining writes: inventory.msisdn.add_range, port_request create/approve/reject,
provisioning resolve/retry/set_fault_injection, promo create/assign, catalog admin
add_offering/add_price/window_offering, usage.simulate (~13). Then the `*.mine`
wrappers + model client + ownership/caps/prompts.

---

**Slice 11 — subscription WRITES.** Seven tools (terminate, purchase_vas, renew_now,
tick_renewals_now, schedule_plan_change, cancel_pending_plan_change,
migrate_to_new_price). Seven new `SubscriptionClient` write methods:
- `terminate_with_reason` reproduces the Python body logic exactly — **no body** when
  `reason=None` + `release_inventory=true` (server defaults `customer_requested`),
  else `{reason?, releaseInventory(only when false)}` (kept the existing raw-body
  `terminate(id, body)` for the crm-service caller).
- `purchase_vas`/`renew`/`tick_renewals_now`/`schedule_plan_change`/`cancel_plan_change`
  are thin; `migrate_to_new_price` is **LLM-hidden** (operator/scenario only; pinned
  in `LLM_HIDDEN_TOOLS`), `effective_from` sent verbatim.
- `subscription.terminate` destructive; `subscription.purchase_vas` explicitly NOT
  (adds allowance) — both pinned.

**Verification:** fmt + clippy clean; workspace green; 7 descriptions byte-pinned.
**Conservative live smoke** (`subscription_writes_live.rs`, `#[ignore]`) green against
tech-vm — a **reversible** `schedule_plan_change → cancel_pending_plan_change`
round-trip on a real subscription (pending set then cleared, seed data unchanged),
plus structured-error paths for terminate/renew/purchase_vas/migrate against bogus
ids (no real termination/charge), and `tick_renewals_now` tolerated (403-or-ok).

**Tool ledger:** ~78/110 (reads complete + customer/case/ticket + subscription writes).
Remaining: order + payment writes (composites: order.create=create+submit,
payment.add_card=tokenize+attach), inventory/port_request/provisioning/promo + catalog
admin. Then the `*.mine` wrappers + model client + ownership/caps/prompts.

---

**Slice 10 — case + ticket WRITES.** Eleven tools (case: open/close/add_note/
transition/update_priority; ticket: open/assign/transition/resolve/close/cancel).
Added 11 `CrmClient` write methods (open_case with the optional description/agent/
transcript-hash args the later `case.open_for_me` needs, store_chat_transcript,
add_case_note, patch_case, close_case, open_ticket, assign_ticket, transition_ticket,
resolve_ticket, cancel_ticket). Two seams:
- **FSM transitions take `{"trigger"}`, not `{"state"}`.** The friendly target-state
  → trigger maps (`CASE_STATE_TO_TRIGGER`, `TICKET_STATE_TO_TRIGGER` +
  `IN_PROGRESS_BY_SOURCE`) live in the tool layer; an unknown target/source yields a
  `ToolError::Other{kind:"ValueError"}` → the exact `{"error":"ValueError","detail":…}`
  observation the graph renders (verified). `ticket.transition`/`ticket.close` cost a
  `get_ticket` read to resolve `in_progress` (three triggers land there). A shared
  `py_list_repr` renders the "valid targets" list Python-style (single-quoted).
- `case.close` + `ticket.cancel` are destructive — pinned by
  `case_ticket_writes_are_operator_and_destructive_gated`.

**Verification:** fmt + clippy clean; workspace green; 11 descriptions byte-pinned.
**Mutating live smoke** (`case_ticket_writes_live.rs`, `#[ignore]`) green against
tech-vm: case open→note→priority→**transition (trigger body accepted)**→unknown-state
**ValueError**; ticket open→resolve→close→case close — the `{"trigger"}` bodies the
prior `{"state"}`/`{"toState"}` shapes 422'd on are accepted.

**Tool ledger:** ~71/110 (reads complete + customer/case/ticket writes). Remaining
writes: subscription/order/payment, inventory/port_request/provisioning/promo +
catalog admin. Then the `*.mine` wrappers + model client + ownership/caps/prompts.

---

**Slice 9 — customer + interaction WRITES (writes begin).** Seven tools
(`customer.create/update_contact/add_contact_medium/remove_contact_medium/
attest_kyc/close`, `interaction.log`) in `register_customer_write_tools`. Writes
carry real body-construction logic (not thin wrappers), so this exercises the
**request bodies live** (the 4c lesson). Added six `CrmClient` write methods (+
`chrono` dep for the `attest_kyc` `verified_at` timestamp): `create_customer`
(name split into given/family + contact-medium defaults), `update_customer`,
`close_customer`, `add_contact_medium`, `remove_contact_medium` (204-empty →
`{id, removed}`), `attest_kyc` (ports the full stub-default body — per-customer
`document_number` from the id's digit tail, `provider_reference`, stub
`attestation_payload`), `log_interaction`. Two tools (`remove_contact_medium`,
`customer.close`) are destructive — pinned by
`customer_writes_are_operator_and_destructive_gated`.

> **⚠️ Owed oracle fix discovered (do NOT fix in the port).** The live smoke
> found a **pre-existing Python client/service mismatch**:
> `customer.add_contact_medium` — the Python **client** wraps the value in
> `characteristic` (`{emailAddress}`/`{phoneNumber}`), but the CRM service route
> binds `AddContactMediumRequest`, which requires a **top-level `value`** (reads
> `body.value`). So the tool **422s in the all-Python world too** — it is a latent
> Python bug, not a port regression. Per R5/behaviour-frozen, the Rust client
> reproduces the `characteristic` body faithfully (and thus the 422); the fix
> belongs in the **Python oracle first** (align the client to send `value`, or the
> service to accept `characteristic`), then re-port. Flagged in the client doc
> comment + the write smoke asserts the reproduced 422. **Owed, like the SOM
> lost-update backport.**

**Verification:** fmt + clippy clean; workspace green (incl. the destructive-gating
+ profile unit test); 7 descriptions byte-pinned. **Mutating live smoke**
(`customer_writes_live.rs`, `#[ignore]`) green against tech-vm: create (body
accepted, real id) → `attest_kyc` (**customer verified** — the ported stub body
works) → `update_contact` → `log_interaction` (the camelCase `customerId` body the
4c bug tripped on — accepted) → `add_contact_medium` reproduces the Python 422 →
`close` (status→closed). Creates then closes one customer.

**Tool ledger:** ~60/110 (reads complete + the first write family). Remaining
writes: case/ticket, subscription/order/payment, inventory/port_request/
provisioning/promo/catalog-admin. Then the `*.mine` wrappers + model client +
ownership/caps/prompts.

---

**Slice 8 — trace + knowledge (the last, infra-heavy reads).** Five tools; the two
read families that need new infra rather than a plain HTTP client:
- **trace** (`trace.get` / `trace.for_order` / `trace.for_subscription`). New
  clients: **`JaegerClient`** (plain reqwest — Jaeger's query API is *outside* the
  BSS token perimeter — `get_trace` + `JaegerError`, `BSS_JAEGER_UI_URL` default) and
  **`AuditClient`** (BssClient-based, `list_events` unwrapping the `{"events": …}`
  envelope). Ported `_summarize_trace` (sorted unique services, **error-TAG** count —
  a tag count despite the `errorSpanCount` name — and `totalMs` from min-start/
  max-end spans) and `_latest_trace_id` (`reversed`, first truthy `traceId`/
  `trace_id`). A Jaeger miss → the `JAEGER_ERROR` dict (returned Ok, not a turn
  failure); no recorded trace → `NO_TRACE_RECORDED` sentinel. `totalMs` uses
  half-away rounding (live-timing derived, never fixture-pinned — noted at the seam).
- **knowledge** (`knowledge.search` / `knowledge.get`) — backed by the **already-
  ported `bss-knowledge` crate** (`search_fts`/`get_chunk`), so the tools take a
  `sqlx::PgPool` (added `bss-knowledge` + `sqlx` to orchestrator deps). Registration
  is caller-gated on `BSS_KNOWLEDGE_ENABLED` (the Python `_maybe_register` contract).
  The `knowledge.get` NOT_FOUND message replicates Python's `{anchor!r}` **single-
  quote** repr byte-for-byte — extracted to `not_found_message()` and **unit-tested**
  against an independent single-line oracle (the `\`-continuation could otherwise
  drift silently). Both operator_cockpit-only (doctrine guard 15).

The description test's two registry-building cases became `#[tokio::test]` (the lazy
`PgPool` needs a runtime to construct; no connection is made).

**Verification:** fmt + clippy clean; workspace green (incl. the new NOT_FOUND unit
test); 5 descriptions byte-pinned. **Live smoke** (`trace_knowledge_live.rs`,
`#[ignore]`) green against tech-vm: `trace.get` bogus → JAEGER_ERROR, `trace.for_*`
bogus → NO_TRACE_RECORDED via the audit path; `knowledge.search` returns `{hits,
query}` against the live FTS index, round-trips the first hit through `knowledge.get`,
and a bogus chunk → the NOT_FOUND sentinel.

**Reads are DONE — ~53/110 tools ported. The entire read surface is now Rust.**
Remaining: all the **writes** (~45, one big slice; destructive gating exists in
`safety.rs`), the **`customer_self_serve` `*.mine`** wrappers (auth binding +
ownership + `_annotate_pricing`), the **OpenRouter `ChatModel`** client, and the
**ownership trip-wire + chat caps + validate_profiles + prompts** slice → then the
`v2.0.0-phase.5` tag.

---

**Slice 7 — the CRM/catalog read BATCH (ticket / case / promo / port_request).**
Eight tools, all verbatim client wrappers except `case.show_transcript_for` (a small
composite: read the case, follow its `chatTranscriptHash` to the transcript, else
return the `{transcript: null, reason: "no_transcript_linked"}` sentinel — key order
via D9). Extended `CrmClient` with `get_case`/`get_chat_transcript`/`get_ticket`/
`list_tickets`/`list_port_requests`/`get_port_request`, and **widened `list_cases`
with `agent_id`** (`assignedAgentId` — the `customer.get` composite caller updated to
pass `None`). Added `CatalogClient::get_promotion`. All operator_cockpit-only (the
chat surface sees only `case.list_for_me`/`case.open_for_me`). Verification: fmt +
clippy clean; workspace green; 8 descriptions byte-pinned; one broad live smoke
(`crm_reads_live.rs`) green against tech-vm — ticket/case/port_request list+get
verbatim, `case.show_transcript_for` returns a body-or-sentinel, unknown promo/ticket
→ `CLIENT_ERROR`.

**Tool ledger:** ~48/110. Remaining reads: **trace** (Jaeger + audit client +
summarizer) and **knowledge** (sqlx pool + the ported `bss-knowledge` crate + the
enablement gate) — both infra-heavy, next batch. Then all the **writes** and the
**`customer_self_serve` `*.mine`** wrappers.

---

**Slice 6 — the operator read BATCH (order / SOM / inventory / provisioning /
usage / agents / events).** Cadence change (per the human): the read families are
retired-risk boilerplate now, so this is one batch of **17 tools** rather than seven
per-family slices. All verbatim client wrappers except two:
- **`order.wait_until`** — a **polling composite** on `ComClient`: loops `get_order`
  until the target (or terminal `failed`/`cancelled`) state or the deadline, then
  returns `ClientError::Timeout` (→504 observation, matching Python's `Timeout`).
  Wall-clock polling (`Instant` + `tokio::time::sleep`), deliberately not the virtual
  clock — mirrors Python's `time.monotonic` + `asyncio.sleep`. Needed `tokio` as a
  normal `bss-clients` dep.
- **`events.list`** — the v0.1 `NOT_IMPLEMENTED` stub; echoes the filter args after
  the base `error`/`message` (key order via D9). The stub message is embedded
  byte-for-byte (verified equal to Python's `_EVENTS_NOT_IMPLEMENTED`).
- **`inventory.msisdn.list_available`** — the one arg subtlety: `status` defaults to
  `"available"` when the key is **absent**, but an explicit `null` means "any state"
  (Python's `status: str | None = "available"`); `opt_str` collapses both, so the
  three cases are decoded by hand.

New clients (consumer-driven, mirroring the catalog/CRM pattern): **`ComClient`**
(get_order/list_orders/wait_until), **`ProvisioningClient`** (get_task/list_tasks),
**`MediationClient`** (list_usage); extended **`SomClient`** (get_service_order/
get_service/list_services_for_subscription), **`InventoryClient`** (list_msisdns/
count_msisdns/list_esims/get_activation_code), **`CrmClient`** (list_agents). The
whole batch is operator_cockpit-only (pinned by `operator_read_batch_is_in_operator_
profile`).

**Verification:** fmt + clippy clean; workspace green, no regression. Descriptions
byte-pinned against the golden (17 new). **One broad live smoke**
(`operator_reads_live.rs`, `#[ignore]`) ran green against the tech-vm stack: inventory
counts/lists/get, esim list, provisioning tasks, usage, agents, the events stub, and a
resolved order→service-order and subscription→service chain — each verbatim tool equal
to a direct client call; `order.wait_until` returns immediately on an already-reached
state; unknown order/task → `CLIENT_ERROR`.

**Tool ledger:** ~40 of 110 tools ported (clock 4, catalog 6, CRM 6, subscription 4,
payment 3, + this batch's 17). Remaining: the **trace** reads (Jaeger + audit client),
**ticket/case/promo/port_request/knowledge** reads, all the **writes**, and the
**`customer_self_serve` `*.mine`** wrappers.

---

**Slice 5 — the payment read family.** Ported three operator_cockpit read tools:
`payment.list_methods` (already had the client method), `payment.get_attempt`,
`payment.list_attempts` — all verbatim. Extended `PaymentClient` with `get_payment`
and `list_payments` (`limit` always sent first, then optional `customerId`/
`paymentMethodId` — preserving Python's `params` seed order; `encode` copied for the
query filters).

- **Live smoke caught a real service contract:** the payment list route requires
  `customerId` (Python `customerId: str`, **no default** — `services/payment/app/api/
  tmf/payment.py`), so an unfiltered `list_attempts()` 400s on *both* Python and Rust
  (the tool signature allows `customer_id=None`, but httpx omits the param and the
  service rejects it — a pre-existing Python quirk the port reproduces faithfully).
  The smoke was corrected to always pass a customer; the parity itself is intact. A
  small reinforcement of the HANDOFF "exercise real service behaviour, not just the
  happy path" lesson.
- Payment reads are operator_cockpit-only (chat sees `payment.method_list_mine` /
  `payment.charge_history_mine`). Pinned by `payment_canonical_reads_are_operator_only`.
- **Verification:** fmt + clippy clean; workspace green, no regression;
  `payment_tools_live.rs` (`#[ignore]`) green against tech-vm — verbatim reads equal
  direct client calls, unknown attempt → `CLIENT_ERROR`. Payment writes (`add_card`
  with its sandbox tokenizer, `remove_method`, `charge`) land with the write slice.

Running client-port ledger (P5c): now covers catalog, CRM, subscription, and payment
reads. Still unported and needed by later families: a **ComClient** (order reads +
the `order.wait_until` polling composite), a **MediationClient** (usage reads), SOM
service reads, inventory/provisioning/knowledge, and the write surfaces.

---

**Slice 4 — the subscription read family + the key-ordering resolution (D9).**
Ported four operator_cockpit read tools: `subscription.get`,
`subscription.list_for_customer`, `subscription.get_balance`,
`subscription.get_esim_activation`. First three verbatim; `get_esim_activation` is
the first **projected-dict** tool (the client reads the subscription and projects
`{subscriptionId, iccid, msisdn, activationCode, imsi}` — no dedicated endpoint,
mirroring the Python client).

- **Resolved the R2 key-ordering seam flagged in slice 3 → D9: enabled `serde_json`
  `preserve_order` workspace-wide.** Python preserves dict insertion order
  everywhere; Rust's default `Value` (BTreeMap) sorts keys, so the ReAct loop's
  `Value::to_string()` observation would diverge from the Python transcript the R2
  gate replays — and a projected-dict tool would emit visibly-reordered JSON. The
  `preserve_order` feature swaps `Value`'s map for `IndexMap`, matching Python for
  *both* verbatim reserialization and `json!` literals at once. **Verified zero test
  breakage:** the whole workspace stays green because every service golden diff is
  `Value ==` (order-independent — `get_json` parses); the three already-ported live
  smokes (catalog/CRM/subscription) re-ran green against the stack. See
  `04-RISKS-AND-DECISIONS.md` D9 for the full rationale.
- Extended `SubscriptionClient` with `get_balance` and `get_esim_activation`
  (`get`/`list_for_customer` were already ported P1–P2). The projected dict is built
  with `json!` in Python dict-literal order; missing fields → `null` (mirroring
  `sub.get(...)`).
- **Live smoke** (`subscription_tools_live.rs`, `#[ignore]`, ran green against
  tech-vm): verbatim reads equal direct client calls; **D9 is pinned by asserting the
  serialized `get_esim_activation` observation carries its five keys in insertion
  order, not alphabetical** — a regression of `preserve_order` fails this test.
  Subscription writes + the `*.mine` chat wrappers stay for later slices.

---

**Slice 3 — the CRM read family + shared tool helpers.** Second application of the
slice-2 template, plus the first **composite** tool. Ported six read tools:
`customer.get`, `customer.list`, `customer.find_by_msisdn`, `customer.find_by_email`,
`customer.get_kyc_status`, `interaction.list`.

- **`customer.get` is a 360 composite** — four independent reads fanned out with
  `futures_util::future::join4` (CRM customer + cases + interactions, Subscription
  line list), mirroring the Python `asyncio.gather(..., return_exceptions=True)`
  exactly: the customer read is the **hard error** (a real NotFound the caller must
  see); the three sub-reads degrade to `[]` on any failure (`ok_array` = the Rust
  shape of `x if isinstance(x, list) else []`) and stitch under the synthetic
  `_extras` key the cockpit's 360 renderer expects. The other five return the client
  response **verbatim**.
- Extended `CrmClient` with `find_customer_by_msisdn`, `find_customer_by_email`,
  `list_customers(state, name_contains)`, `get_kyc_status`, `list_cases(customer_id,
  state)`, `list_interactions(customer_id, limit)` — each a consumer-driven addition
  mirroring the catalog extension. Python's param mapping preserved (`state`→`status`,
  `name_contains`→`name`; each sent only when present). Added a private `encode` (a
  copy of `catalog::encode`) so email `+` addressing survives the query string.
  `SubscriptionClient::list_for_customer` (already ported P1) backs the composite.
- **Refactor:** promoted `map_client_err` / `req_str` / `opt_str` from `catalog.rs`
  to `tools/mod.rs` as `pub(crate)` — the shared client-backed-tool helper kit;
  `catalog.rs` now imports them (no behaviour change, tests re-verify).
- **Profile placement:** the canonical CRM reads are **operator_cockpit-only** — the
  chat surface sees only the ownership-bound `*.mine` wrappers (a later slice), never
  these unscoped reads. Pinned by a new `crm_reads_are_operator_only` test (present in
  operator_cockpit, absent from customer_self_serve, both directions).

**R2 open item flagged this slice — tool-observation key ordering.** The agent
serializes a tool result via `Value::to_string()`, and the workspace's `serde_json`
has **no `preserve_order` feature**, so object keys serialize **alphabetically
sorted**, whereas Python (`httpx.json()` → dict → `json.dumps`) preserves server /
insertion order. For **verbatim** tools this only affects the *observation string*,
not `Value ==` (tests stay order-independent, as accepted since slice 2). It first
becomes *observable* in **projected-dict** tools (e.g. `subscription.get_esim_
activation` builds a fixed-key dict) — so those are **deferred to their own slice**,
and the resolution (most likely enabling `preserve_order` workspace-wide and
re-validating the service goldens, or confirming the R2 gate compares the event
sequence rather than byte-exact observation strings) is settled when the
transcript-parity gate is built. Noted here so the decision isn't silently made by a
`json!` key order.

**Verification (slice 3).** fmt + clippy clean (`-D warnings`); **workspace green,
no regression** (the `catalog.rs` helper move + `CrmClient` additions left every
service test untouched). Description golden extended to the six CRM reads (byte-exact
`include_str!` desc files pinned against `tool_descriptions.json`). **Live smoke**
(`tests/customer_tools_live.rs`, `#[ignore]`, ran green against the tech-vm stack):
`customer.list` verbatim + non-empty seed data; `customer.get` returns the requested
doc with an `_extras` object carrying array subscriptions/cases/interactions;
`get_kyc_status` + `interaction.list` + name-filtered `customer.list` each equal a
direct client call; unknown customer → `CLIENT_ERROR`, not a panic.

---

**Slice 2 — the client-backed tool pattern (catalog read family).** The template
for the remaining ~100 tools: a tool is a closure capturing its typed `bss-clients`
client, returning the client response **verbatim** and mapping `ClientError` to the
structured observation (`graph._tool_error_to_observation` — policy→`rule`+detail,
else `CLIENT_ERROR`+status). Byte-parity of the tool output follows **transitively**
from the P3 catalog service golden diff (Rust catalog == Python catalog), so no
re-diff against the Python tool is needed — the live test asserts `tool output ==
direct client call` instead.

- Ported the six catalog **read** tools (`list_offerings`, `get_offering`,
  `list_vas`, `get_vas`, `list_active_offerings`, `get_active_price`), descriptions
  embedded byte-for-byte and pinned against the golden.
- Extended `CatalogClient` with `list_offerings()`, `list_vas()`, and
  `get_active_price_at(id, at)` (the `at`-aware variant — sends `activeAt` only when
  `Some`, matching Python's `params` gate; the existing `get_active_price` delegates,
  so P3/P4 callers are untouched).
- The 3 catalog **admin write** tools (hidden from the LLM) defer with the admin
  client methods.

**Verification (slice 2).** fmt + clippy clean; workspace green (76 groups, no
regression — the client delegation didn't disturb com/subscription). Description
golden extended to the catalog family + **profile-membership** assertions
(operator_cockpit sees all six; customer_self_serve sees only the three public
reads, not `get_active_price`/`list_offerings`) + a `surface()` intersection test.
**Live smoke** (`tests/catalog_tools_live.rs`, `#[ignore]`): each read tool against
the running catalog returns the client response verbatim (asserted equal to a direct
client call) with real data (PLAN_M, offerings, VAS, price rows); unknown offering →
`CLIENT_ERROR`, not a panic.

---

**Slice 1 — the ReAct loop + fixture player + guards.**

`rust/crates/bss-orchestrator` — the LLM agent brain, in-process-linked by the
P6/P7 portals + CLI (never over the network — D3). This is the biggest, hardest
crate (~7.2k Py LOC + 110 tools), so it lands over **several slices**. Slice 1 is
the hardest architectural core proven on the smallest real tool surface:

- **`agent::astream_once`** — the **hand-rolled ReAct loop** that replaces
  LangGraph's `create_react_agent`: system prompt + prior transcript + user →
  model → run `tool_calls` → append tool results → repeat until the model stops
  calling tools. Emits the same `AgentEvent` sequence as the Python stream, incl.
  the full **guard stack**: the 3-strike **failure bail**, the identical-call
  **stuck bail** (`IdenticalCallTracker`), and destructive gating. `TurnUsage`
  emitted before `FinalMessage` (the SSE-ordering lesson). Transcript-rehydration
  parser (`messages_from_transcript`) ported with the 32k-char cap.
- **`chat_model`** — the `ChatModel` seam (generic, so the loop drives either the
  mock or a real client) + the **`MockChatModel` fixture player**: substring-match
  on the latest user message → walk the `steps` array, `mock_call_{n}_{i}` ids
  post-increment. This is the R2 acceptance harness.
- **`safety`** — `DESTRUCTIVE_TOOLS` + `gate_destructive` with `batched`/`granular`
  autonomy + shared `LoopState` (granular re-gates each destructive after the first).
- **`tools`** — the registry + `ToolSpec` + the `customer_self_serve`/
  `operator_cockpit` **profile** sets + the `LLM_HIDDEN_TOOLS` set. Tools are async
  `Fn(Value, ToolCtx) -> Result<Value, ToolError>` (matching Python's "tool is a
  function"). First real family: **`clock.*`** (dependency-free → deterministic).
- **`events`** — the `AgentEvent` enum (PromptReceived / ToolCallStarted /
  ToolCallCompleted / FinalMessage / Error / TurnUsage).

**R2 discipline established.** Tool descriptions are the LLM-facing semantic
contract (a behavioural contract with the model), so a golden `{name: description}`
map for **all 110 tools** was captured from the Python registry up front; the
`clock.*` descriptions are embedded byte-for-byte (`include_str!`) and pinned. Each
future tool family validates its slice against the same golden as it lands.

**Following slices (P5c.2+):** the OpenRouter `ChatModel` client (reqwest, D4-style
direct); the remaining ~106 tools (schemars arg schemas per **D5**, profile by
profile, `customer_self_serve` first) each wrapping a `bss-clients` call; the
ownership trip-wire (`OWNERSHIP_PATHS`) + `chat_caps`; `SYSTEM_PROMPT` +
customer-chat prompt; `validate_profiles()` full-coverage check. The
**fixture-corpus transcript-parity gate (R2)** closes when the tools land.

**Verification.**
- fmt + clippy `-D warnings` clean; workspace tests green (75 groups, no regression).
- **Description golden** (`tests/tool_descriptions.rs`, CI): the `clock.*`
  descriptions byte-for-byte vs the Python registry docstrings.
- **ReAct-loop transcript** (`tests/agent_loop.rs`, CI, frozen clock, no DB/HTTP):
  a fixture drives four transcripts — happy `clock.now` round trip (deterministic
  result under a frozen clock), destructive **block** (and gate-opens under
  `allow_destructive=true`), 3-strike **failure bail**, identical-call **stuck
  bail** — each asserting the exact `AgentEvent` sequence.
- **Safety units**: batched authorises the loop; granular re-gates after the first.

### Phase 5b — bss-cockpit core — ✅ PORTED (2026-07-13)

`rust/crates/bss-cockpit` — the operator-cockpit **core** the orchestrator + both
P6/P7 consumers need. Four modules:

- **`conversation`** — the Postgres-backed `ConversationStore` + `Conversation`
  handle (`cockpit.session`/`message`/`pending_destructive`, alembic 0014). Open/
  resume/list/append(user|assistant|tool)/list_messages/reset/close/set_focus +
  the pending-destructive set/peek/consume (the `/confirm` contract). SES ids are
  `SES-YYYYMMDD-<8hex>`. `transcript_text()` is the **frozen contract** the P5c
  orchestrator will parse — `role:\ncontent` blocks joined by a blank line, tool
  rows carry a `tool[NAME]:` prefix, and assistant **chrome rows are dropped**
  (via `is_cockpit_chrome`, so rehydrated history never feeds the LLM its own
  placeholder output → the v1.5 mimicry/state-confusion/citation-thrash guard).
- **`config`** — `OPERATOR.md` + `settings.toml` loader with **mtime hot-reload**,
  autobootstrap from embedded defaults, and the **last-good fallback** (an editor
  typo serves the prior good view instead of bricking the cockpit). `CockpitSettings`
  covers `[llm]`/`[cockpit]`/`[ports]`/`[dev_service_urls]`.
- **`prompts`** — `build_cockpit_prompt` + `COCKPIT_INVARIANTS`, the code-defined
  safety contract embedded **byte-for-byte** (`include_str!` of the 15.8 KB block
  extracted from the oracle — an R2 behavioural contract with the model).
- **`chrome_filter`** — `is_cockpit_chrome` + the `ASSISTANT_CHROME_PREFIXES`
  inventory (the transcript filter).

**Byte-parity seams.** Two: (1) the verbatim `COCKPIT_INVARIANTS` — extracted to
a file and `include_str!`d rather than retyped, so the prompt golden validates it
exactly; (2) **pending-destructive arg key order** — the prompt renders
`f"{k}={v!r}"` in stored-JSON order, so the store reads `tool_args_json::text`
(the `json` column preserves text order — not `jsonb`) and parses into an
`IndexMap`, and a `py_repr` reproduces Python's string-repr quoting.

**Deferred to P6/P7** (land with their browser/CLI consumers, per land-with-first-
consumer): the ASCII **renderers** (~1.6 KB LOC), `chrome_filter::strip_fake_propose`
+ `postprocess::*` (all use lookbehind/lookahead regexes the `regex` crate can't do
→ `fancy-regex` there), and the `settings.toml`/branding **writers** (land with the
`bss-branding` crate). The `[branding]` table in `settings.toml` is ignored on load
until then (serde skips unknown fields), so an operator's file loads unchanged.

**Verification.**
- fmt + clippy `-D warnings` clean; workspace tests green (no regression).
- **Prompt golden** (`tests/prompt_golden.rs`, CI, no DB): `build_cockpit_prompt`
  byte-for-byte vs the oracle across 5 cases (empty / md+focus / pending-destructive
  / extra-context / all) — which validates the 15.8 KB invariants block — plus the
  `is_cockpit_chrome` behaviour + prefix-inventory lock.
- **Config behaviour** (`tests/config_test.rs`, CI): parse all sections,
  cache-hit, last-good-on-bad-TOML, valid mtime reload, empty-dir autobootstrap.
- **Live store smoke** (`tests/live_smoke.rs`, `#[ignore]`): open→append(mix incl.
  a chrome row)→`transcript_text` contract → structured view → pending-destructive
  round trip with **key-order preservation** → resume → close, against the real
  `cockpit` schema. Self-cleaning (deletes its session + rows).

### Phase 5a — bss-knowledge — ✅ PORTED (2026-07-13)

`rust/crates/bss-knowledge` — the doc-corpus chunker + FTS search backing the
v0.20 cockpit knowledge tools. Four modules mirroring the Python package:

- **`paths`** — `INDEXED_PATHS` allowlist (the doctrine source of truth for what
  the LLM can cite; guard 16), `kind_for`, `kind_rank_weight`. Pinned by golden.
- **`chunker`** — markdown → chunks. The delicate part: GitHub-flavoured anchor
  algorithm (`[^\w\- ]+` Unicode strip → spaces-to-hyphens → trim), per-file
  split policy (`##` default; `##`+`###` for handbook/ARCHITECTURE; dated `##`
  for DECISIONS), frontmatter strip, and the heading-path trail with its exact
  **stack-updated-before-flush** ordering quirk reproduced verbatim (R5:
  behaviour-frozen, quirks included).
- **`search`** — `search_fts` + `get_chunk`. Issues the **identical SQL** so
  `ts_headline`/`ts_rank`/`plainto_tsquery` ranking + snippets are computed in
  Postgres exactly as for the oracle; the only Rust-side logic is the
  kind-weight re-rank multiply + stable re-sort. `indexed_at` renders via
  `bss_clock::isoformat` (`+00:00`, micros-when-nonzero) to match Python
  `datetime.isoformat()`.
- **`indexer`** — the operator-run reindex (3 idempotency layers, deterministic
  `sha256(path|anchor)[:32]` id, delete-stale). Ported for completeness;
  consumed by the P7 CLI. Not run against the live shared table in tests (it
  mutates); the chunker (which produces every upserted row) is golden-pinned.

**The `@type`/datetime/money seams don't recur here** — knowledge is plain text
+ Postgres FTS. The one seam that mattered: `ts_rank` is `REAL` (float4); reading
it as `f32` then widening to `f64` before the weight multiply matches asyncpg's
float4-decode → Python-float path.

**Verification.**
- `cargo fmt` + `clippy -D warnings` clean; workspace tests green (no regression).
- **Chunker golden** (`tests/chunker_golden.rs`, runs in CI, no DB): byte-for-byte
  vs `bss_knowledge.chunker` across the three distinct split policies —
  CLAUDE.md (14), DECISIONS.md (89), HANDBOOK.md (89), ARCHITECTURE.md (37), a
  runbook (6) — plus `INDEXED_PATHS`/kind/weight parity. Anchors, heading-path
  trails (quirk included), and per-file levels all match.
- **Live golden diff** (`tests/live_smoke.rs`, `#[ignore]`): `search_fts` over 6
  queries (incl. an empty-result miss, a `kinds`-filtered scope, and the handbook
  re-rank) + `get_chunk` (hit + miss) against the same live `knowledge.doc_chunk`
  the oracle reads. The exported **wire contract** (`to_value`, which omits
  `rank`) is byte-identical; ordering identical. `rank` itself came back **1 ULP**
  off on one handbook hit (`f32→f64` widen-then-multiply rounding) — it's an
  internal ordering score, not part of the contract, so the test pins the wire
  shape exactly and `rank` within `1e-12`.

**Lesson:** where the heavy lifting is a Postgres builtin (FTS ranking, snippet
generation), byte-parity is structural — the risk concentrates in the pure Rust
around it (the chunker's anchor/trail algorithm, and float widening at the
sqlx boundary). The chunker golden is the high-value test; the live diff is
confirmation.

## Phase 4 — payment → subscription → crm — ✅ COMPLETE (tag `v2.0.0-phase.4`)

The big three, each its own cutover (03-PHASES §Phase 4). Ordered by blast radius.
The phase tag `v2.0.0-phase.4` caps the set after crm; intra-phase cutovers are commits.

### Phase 4c — crm — ✅ PORTED + CUT OVER (2026-07-13)

**crm** — the **last service** — is ported and **cut over into the running stack**.
**The entire service plane is now Rust** (rating + event plane + catalog + com +
payment + subscription + crm); only the portals + orchestrator + CLI remain Python.
Tagged `v2.0.0-phase.4`. ~11 modules covering the widest surface of any service: 4
FSMs, ~13 tables across the `crm` + `inventory` schemas (+ `audit.chat_transcript`).

**Shape — the simplest event-wise, the widest surface-wise.** HTTP-only,
**stage-only events**: the oracle's `publisher.publish` only stages the
`audit.domain_event` row (`published_to_mq=false`) and the lifespan opens **no
broker** — no relay, no consumer, no MQ (like payment). crm events are audit
substrate; the loyalty-registry mirror is a direct HTTP call, not an event. Two
outbound clients: `SubscriptionClient` (`get` / `list_for_customer` / `terminate` —
added this phase) and an optional `LoyaltyClient` (`register_customer` — added,
best-effort, never fails customer creation).

**The inventory pools are the cross-service contract.** crm hosts
`/inventory-api/v1/` (MSISDN + eSIM), which subscription (P4b) and som (P2) already
call via `InventoryClient`. Those surfaces — reserve-next (`FOR UPDATE SKIP
LOCKED`), assign/release/recycle, the eSIM FSM transitions, `mark_ported_out`
(terminal `ported_out` + far-future quarantine) — port byte-for-byte so the
already-cut services keep working unchanged.

**Domains ported:** TMF629 customer (create → party+individual+customer+CMs, the
email-unique + deactivation guards, contact-medium/individual updates, by-msisdn →
subscription → customer resolution, by-email), TMF621 ticket + its 7-state FSM,
TMF683 interaction (auto-logged on every customer/case/ticket write), Case FSM
(resolve-needs-all-tickets-resolved, cancel-cascades-to-tickets, close
fast-forwards through resolve), KYC attestation (Didit corroboration-row check +
freshness window; prebaked/legacy gated on `BSS_KYC_ALLOW_PREBAKED`; raw-doc → last4
+ SHA-256 reduction; doc-hash uniqueness with the sandbox re-link affordance),
PortRequest MNP (port-in seeds the pool, port-out flips to `ported_out` +
terminates the sub with `releaseInventory=false`), agent reads, hash-addressed chat
transcripts.

**Byte-exactness seams (P3/P4 lessons, reused).** TMF projections render `@type` +
`Z` datetimes (micros-when-nonzero) + camelCase; internal DTOs are snake_case
(case/agent/inventory/kyc), port-request camelCase; `date` fields render ISO
`YYYY-MM-DD`. **Relationship-backed collections carry NO `ORDER BY`** —
`contactMedium`, case `notes`, `ticket_ids` mirror the oracle's un-ordered
`selectinload` (physical/insertion order), the same lesson as the subscription
balances (the one golden-diff miss, fixed). The admin reset owns **two schemas**:
`crm` operational truncate + the `inventory` pools **UPDATE-reset** (rows kept,
assignment cleared) via `TableReset::update`.

**Cutover note — one write-body bug the read golden diff missed.** crm has no
consumer/relay so the swap itself was clean (no queue reconciliation like 4b). But
the two LLM blocked-subscription hero scenarios first failed: `POST /interaction`
(TMF683) 422'd on the camelCase `customerId` the cockpit/agent sends. The oracle's
`CreateInteractionRequest` extends `TmfBase` (`to_camel` + `populate_by_name`) so it
accepts both cases; the Rust struct only accepted snake_case. The agent thrashed on
the 422 (→ the 90s turn timeout + the missing `portal-csr` interaction assertion).
Fixed by `#[serde(rename_all = "camelCase")]` + snake aliases (commit `2ecd927`);
both scenarios then passed at normal speed (25s / 12s, down from 95s / 116s). The
read-only golden diff doesn't cover request bodies — a lesson for P5: exercise the
write surface too.

**Verification.**
- fmt + clippy `-D warnings` clean; **4 FSM unit tests**; workspace test suite green
  (62 groups, no regression from the `bss-clients` additions).
- **Live golden diff** (`tests/live_smoke.rs`, `#[ignore]`): every read endpoint
  byte-identical to the Python oracle — customer (single/list/by-email/404), the
  inventory pools (msisdn single/list/count, esim single/list/activation), ticket,
  case, agent, interaction, kyc-status, port-requests; token perimeter matches.
- **Hero suite: 15/19** — every crm-touching scenario green (signup creates
  customer+KYC+inventory, port-in/out, inventory low-watermark, cockpit case/ticket
  handling). The 4 failures are the exact same pre-existing portal/trace issues as
  the 4a/4b baseline (branding text, `/auth/check-email` 400, Jaeger `spanCount`) —
  **zero regression**. (payment flipped to mock for the run, as the harness intends.)
- Stack fully healthy afterward: **all 8 services Rust** + both portals all 200;
  payment restored to stripe mode.

**This is the bilingual resting point (`v2.0.0-phase.4`):** an all-Rust service
plane behind all-Python portals/orchestrator/CLI. Next is P5+ (portals, orchestrator,
CLI) per `03-PHASES.md`.

### Phase 4b — subscription — ✅ PORTED + CUT OVER (2026-07-13)

**subscription** is ported and **cut over into the running stack** (Rust image). Service
plane is now Rust for rating + event plane + catalog + com + payment + subscription;
only **crm remains Python**. ~3.9k Rust LOC (16 modules) + a `bss-clients` surface
extension (`PaymentClient::charge`, `CatalogClient::{get_offering_price,
list_active_offerings,get_vas}`, `InventoryClient::{get_msisdn,get_esim,assign_msisdn,
assign_msisdn_to_esim,recycle_esim}`).

**Shape.** The richest of the P4 trio: runs the **outbox relay** (its staged events'
only publisher) + the **usage.rated safe consumer** + the **in-process renewal worker**
— the full com-style event topology, plus HTTP write paths.

**Pure domain core (10 unit tests).** `domain.rs` ports `bundle` (consume/is_exhausted/
add_allowance/reset_for_new_period, `UNLIMITED=-1`) + the 4-state FSM (pending/active/
blocked/terminated) as pure functions. `money.rs` reuses catalog's byte-identical
`apply_discount` (round-half-up 2dp). All block-on-exhaust + discount-counter logic is
unit-tested against the oracle.

**Block-on-exhaust (the crux).** `handle_usage_rated` runs on the safe consumer's
`&mut PgConnection` (bind_consumer owns the commit) with the balance row
`SELECT … FOR UPDATE` — the decrement serialization. In sqlx each query hits Postgres
directly (no identity-map cache), so the oracle's load-bearing `populate_existing=True`
fix is **structurally free**. Roaming (`data_roaming`) is policy-gated independently and
never exhausts the subscription (v0.17 doctrine).

**Renewal worker (v0.18).** `worker.rs` ports the tick loop: `sweep_due`
(`SELECT FOR UPDATE SKIP LOCKED` + commit the `last_renewal_attempted_at` **mark before
the row lock releases** → multi-replica no-double-charge), then `service::renew` per id
in its own tx; `sweep_skipped` emits `subscription.renewal_skipped` for blocked+overdue.
The admin `/renewal/tick-now` (gated by `BSS_ALLOW_ADMIN_RESET`) drives one deterministic
sweep for the renewal hero scenario. **The v0.18 upcoming-renewal *reminder* sweep is
intentionally not ported** — it needs the portal email adapter (lands with portals in
P6); this mirrors the oracle path when `email_adapter is None` (sweep disabled,
`renewal_reminder_sent_at` untouched — not an API-observable field).

**Renewal / plan-change pivot.** `renew()` charges the **price snapshot** on the row
(never the catalog), applies the promo discount while the per-sub counter is live,
decrements it (perpetual `-1` never decrements); on a due pending plan-change it pivots
offering + snapshot + resets the bundle to the new plan's allowances and clears the
promo (a plan change ends the promo). Price migration stamps per-sub pending fields +
per-sub events (no batch UPDATE that loses the audit trail).

**Money + datetime seams (P3 lessons, reused).** `price_amount`/`discount_value` read as
`::text` → `Decimal`, rendered as 2dp **strings**; `effectiveAmount` computed via
`apply_discount`; TMF response datetimes render `Z` (micros only when nonzero); event
payloads render `+00:00` via `bss_clock::isoformat`. Balances serialize in **insertion
order** (no `ORDER BY` — matches the oracle's un-ordered selectinload). `@type` renders
as `atType` (the oracle's `to_camel("at_type")`, captured off the live wire).

**Cutover note — the one queue-topology snag.** subscription is the **only** service
whose Python consumer used a plain `declare_queue` for `usage.rated` (never migrated to
the v1.2 safe-consumer pattern, though its config knobs were provisioned for it). com/som
already used the shared `bss_events.bind_consumer` (retry topology), so their cutovers
matched. The Rust port correctly adopts `bind_consumer` like com/som — but RabbitMQ
refuses to redeclare the existing plain queue with the added `x-dead-letter-exchange`
arg (`PRECONDITION_FAILED`). **Fix (one-off, subscription-specific):** delete the
orphaned, empty `subscription.usage.rated` + `subscription.notification.logger` queues
(0 messages, 0 consumers — Python is gone) so the Rust safe-consumer redeclares
`usage.rated` (+ `.retry`/`.parked`) cleanly. The `notification.logger` stdout logger is
not ported (no API/DB effect — the durable `audit.domain_event` row is the substrate).

**Verification.**
- fmt clean, clippy `-D warnings` clean, **10 subscription unit tests** green; workspace
  test suite green (no regression from the `bss-clients` extension across the other 6
  services).
- **Live golden diff** (`tests/live_smoke.rs`, `#[ignore]`): every read endpoint
  byte-identical to the Python oracle (subscription single, list-for-customer, by-msisdn,
  balance, + 404 envelopes) — covers balances insertion-order, `priceAmount`/
  `effectiveAmount` strings, discount fields, `Z` datetimes, `atType`; token perimeter
  matches (health exempt / 401 / 200).
- **Hero suite: 15/19** (auto/LLM mode) — every subscription-touching scenario green:
  `customer_signup_and_exhaust` (block-on-exhaust), `customer_renews_automatically`
  (renewal worker + `tick-now`), `customer_buys_roaming_and_uses_it` (roaming VAS),
  `catalog_versioning_and_plan_change` (plan-change pivot),
  `operator_port_out_terminates_subscription` (terminate),
  `operator_cockpit_handle_blocked_subscription`, `llm_troubleshoot_blocked_subscription`,
  `new_activation_with_provisioning_retry`. The **4 failures**
  (`portal_self_serve_signup_direct`, `portal_login_with_step_up`,
  `portal_post_login_self_serve`, `trace_customer_signup_swimlane`) are the **exact same
  4 that fail on the pre-cutover / 4a baseline** (portal branding text, `/auth/check-email`
  400, Jaeger `spanCount`) — none subscription-related → **zero regression**.

**Cutover gotcha #1 — payment provider.** The hero suite creates **mock** payment
methods, so the harness (`make scenarios-hero`) flips `BSS_PAYMENT_PROVIDER→mock` for the
run and restores it after. Running `bss scenario run-all` **directly** skips that flip; with
the live payment container in stripe mode, every activation/renewal charge trips the
v0.16 lazy-fail guard (`token_provider='mock'` vs active `StripeTokenizerAdapter`) and the
`service_order.completed` handler parks — an artifact, not a subscription bug. Flip
payment→mock (recreate `--no-deps`), run, then restore to stripe.

**Cutover gotcha #2 (unchanged from P2/P3/4a).** `make scenarios-hero`'s provider-flip
force-recreates `portal-self-serve`, which health-`depends_on` the Rust services (no
HEALTHCHECK until P8) and strands it. Ran scenarios **directly** with the overlay held and
the portal already up. P8 (binary healthchecks) resolves this properly.

### Phase 4a — payment — ✅ PORTED + CUT OVER (2026-07-12)

**payment** is ported and **cut over into the running stack** (Rust image, stripe-mode
— the live deployed config). Service plane is now Rust for rating + event plane +
catalog + com + payment; only subscription/crm remain Python. ~1.9k Rust LOC (14
modules) + the `PaymentClient` surface extension deferred to 4b (com only needs
`list_methods`, already present).

**Shape.** HTTP-only, like catalog — **no MQ, no relay**: the oracle's
`publisher.publish` only stages the `audit.domain_event` row (`published_to_mq=false`)
and returns; the lifespan opens no broker connection. `events::stage` replicates this
exactly. So payment is the simplest event-wise of the P4 trio.

**The tokenizer seam.** The oracle's `TokenizerAdapter` Protocol → a closed `Tokenizer`
enum (mock | stripe), avoiding an `async-trait` dep. Mock preserves the
`tok_FAIL_*`/`tok_DECLINE_*` decline affordances. **Stripe via direct reqwest
(Decision D4** — the Python `stripe` SDK doesn't port): PaymentIntent create
(`off_session`+`confirm`, `Idempotency-Key` header), customer ensure (cached in
`payment.customer`), attach/detach (test-card relink under `ALLOW_TEST_CARD_REUSE`),
retrieve card; every call recorded to `integrations.external_call` with the ported
`_redact_stripe`. **The live container is stripe-mode**, so this path is load-bearing
(not a mock-only shortcut). `select_tokenizer`'s four fail-fast startup guards ported
verbatim (unknown provider; missing creds; prod + `sk_test_*` / mode mismatch;
`ALLOW_TEST_CARD_REUSE` + `sk_live_*`).

**Webhooks.** The Stripe receiver (`/webhooks/stripe`, exempt from the token perimeter
by path) ports `bss_webhooks`: `_verify_stripe` (HMAC-SHA256 over `{t}.{body}`,
timestamp-skew, constant-time hex compare via `subtle`), `WebhookEventStore` dedup on
`(provider,event_id)`, and the routing — reconcile / **drift-not-overwrite** (webhook is
secondary truth) / refund + dispute **record-only** (motto #1). 5 signature unit tests.

**Money + datetime seams (P3 lessons, reused).** `amount` read as `amount::text` →
`Decimal`, rendered as a 2dp **string** on the wire; TMF response datetimes render `Z`
(micros only when non-zero) via a local `tmf_datetime`. Captured the live wire first.

**Verification.**
- fmt clean, clippy `-D warnings` clean, **15 payment unit tests** green (workspace 148 → 163).
- **Live golden diff** (`tests/live_smoke.rs`, `#[ignore]`): every read endpoint
  byte-identical to the Python oracle (payment single/list/filtered/count, paymentMethod
  single/list, both 404 envelopes); token perimeter matches (health exempt / 401 / 200).
- **Full hero suite run directly** against the whole stack with payment=mock (Rust):
  **15/19 PASS**, incl. all payment-critical ones (signup_and_exhaust 13/13, renews 18/18,
  roaming VAS, activation-with-retry). The 4 FAIL are portal-login/branding/Jaeger-trace
  scenarios (`/welcome` custom-branding text, `/auth/check-email` 400, `spanCount` None) —
  **verified to fail identically on the pure-Python-payment baseline**, so zero regression
  from the port (Playbook "red baseline = environment, not the port").
- Deployed container logs clean `INFO` (`service.starting … payment_provider=stripe`),
  `grep -icE 'password|PLAIN|NOT_ALLOWED|panic'` → 0.

**Deployment note (the P2/P3 gotcha, reconfirmed + worked around).** `portal-self-serve`
health-`depends_on` payment (+catalog/com/som), and the Rust images have **no HEALTHCHECK
until P8** — so `make scenarios-hero`'s provider-flip `--force-recreate portal-self-serve`
leaves the portal stuck in `Created` (its Rust deps never report "healthy"). Fix, as in
P2/P3: run scenarios **directly** (`bss scenario run[-all]`) with the overlay held, and
start the portal with `docker compose … up -d --no-deps portal-self-serve` to bypass the
gate. The `make scenarios-hero` path stays red on the Rust-heavy stack until P8 adds
binary healthchecks. Overlay "cut over so far" now includes payment.

**Next (4b): subscription** — highest correctness stakes (double-billing + quota math);
renewal worker, balance decrement under `FOR UPDATE`, price-snapshot renewal, VAS,
proptest the hypothesis balance suite.

---

## Phase 3 — catalog + com — ✅ COMPLETE (tag `v2.0.0-phase.3`)

Two services ported and **cut over into the running stack**. The service plane is
now Rust for rating + the event plane + catalog + com; only subscription/crm/payment
remain Python. ~4.6k Rust LOC across two crates + six new typed clients/methods.

**catalog** (HTTP-only — no MQ, no consumer, no audit/reset router; just a pool + an
optional `LoyaltyClient`): TMF620 read surface (offering/price/spec) + VAS + admin
writes (add-offering/window/retire/add-price) + the v1.1 **promotion subsystem** (the
two-system create saga over the external loyalty-cli, targeted assign/unassign,
exhaust, validate/preview/resolve reads). **com**: TMF622 ProductOrder FSM
(create → submit → completed/failed/cancelled), price snapshot at order time, the
v1.1 promo consume lifecycle at activation (claim → redeem / revoke), the outbox
relay + two safe consumers (`service_order.completed/failed`) + the reconciliation
sweeper.

**The R1 money seam (the headline of P3).** `rust_decimal` added to the workspace;
money columns (`NUMERIC`) are read as `amount::text` → `Decimal::from_str` so the 2dp
scale is preserved exactly. `apply_discount` (round-half-up to 2dp) and
`discount_label` (`normalize()` for "20% off"; `{:.2}` for "SGD 5.00 off") match
`bss_models.discount` byte-for-byte. Two **distinct datetime seams** now coexist and
must not be confused:
- **TMF response bodies** render `Z` (Pydantic v2 default: `2026-04-01T00:00:00Z`,
  fraction omitted when zero) — the `tmf_datetime` formatter in each service.
- **Event payloads + policy-message strings** render `+00:00` micros —
  `bss_clock::isoformat` (the P2 seam), e.g. the no-active-price 422 message.
- **Money on the wire is mixed:** TMF `Money.value` is a JSON **float** (`25.0`);
  `discountValue` / order `priceAmount` are Pydantic `Decimal` → JSON **strings**
  (`"20.00"`, `"25.00"`). A third subtlety: com's create path reproduces Python's
  `Decimal(str(value))` where `value` is a catalog JSON float — `Value::to_string()`
  gives the seed string "25.0" (not "25"), so the `order.acknowledged` event payload
  matches; the DB row then reads back "25.00".

**New clients (each partial to the calls the phase needs):** `LoyaltyClient` (its own
transport — bearer + `X-Actor-Id`/`Idempotency-Key`, `POST /v1/tools/<name>`, the
refusal-422 → `ClientError::Policy` envelope), `CrmClient::get_customer`,
`PaymentClient::list_methods`, `SomClient::list_for_order`,
`CatalogClient::{get_active_price, validate_promo, resolve_eligible_promo}`,
`SubscriptionClient::create`. Loyalty **is enabled** in this stack, so the promotion
saga runs live; catalog and com each hold their own client (token never leaves the
process).

**SOM P2 lock lesson applied.** com's consumer handlers read the order aggregate
`FOR UPDATE` and the safe consumer processes serially — the same serialize/lock
discipline the P2 SOM port introduced. (The **Python-side backport** of the SOM CFS
`pendingTasks` race is still owed; noted again here.)

**Validation.**
- **Golden diff (catalog):** the Rust catalog, booted in-process against the same
  live Postgres + loyalty, was diffed (`Value ==`, order-sensitive) against the live
  Python oracle across 20+ endpoints — every TMF620 read (list/filtered/activeAt/get/
  404), both price paths, specs, VAS, TMF671 promotions, and the live-loyalty promo
  reads (validate valid+invalid, preview, customer-offers) — **all byte-identical**.
  The only endpoint pulled out of the strict loop is the no-active-price 422, whose
  message carries `clock_now()` (differs by ms between two live calls); its shape
  matches (asserted field-by-field). com's read surface (order get/list/404) was
  golden-diffed the same way.
- **Write paths (catalog):** exercised inertly against the deployed Rust container
  (add-offering → add-price with `retire_current` rollover → active-price resolves to
  the new row → admin-gate 422 on anonymous actor), then cleaned up via psql.
- **Hero scenarios:** all six P3-relevant deterministic scenarios green against the
  confirmed all-Rust order plane (overlay held) — both named exit criteria
  (`catalog_versioning_and_plan_change`, `new_activation_with_provisioning_retry`)
  plus `customer_signup_and_exhaust`, `operator_adds_roaming_plan`,
  `customer_buys_roaming_and_uses_it`, `customer_renews_automatically`.
- **Deployed-log scan:** com + catalog both clean (`password|PLAIN|NOT_ALLOWED|panic|
  ERROR` → 0); com's two consumers + outbox relay start clean.

**Deployment gotcha (same as P2), with the clean workaround proven:** run scenarios
with `COMPOSE_FILE=docker-compose.yml:docker-compose.rust.yml` exported — the
provider-flip recreate (`up -d --force-recreate portal-self-serve crm payment`) then
resolves against the overlay and leaves the Rust images in place. Verified: all six
Rust services stayed Rust through the flip; payment/crm/portal recreated as Python.

### Phase 2 → Phase 3 (this work)

Tagged `v2.0.0-phase.2` → next was **Phase 4 (payment → subscription → crm)**.

---

## Phase 2 — Event-plane services: mediation, provisioning-sim, som — ✅ COMPLETE (tag `v2.0.0-phase.2`)

Three services ported and **cut over into the running stack**, plus the deferred
lapin/sqlx event-plane bindings (relay tick loop + safe retry/park consumer). The
order pipeline now runs on an all-Rust event plane (mediation → rating →
subscription; com → som → provisioning-sim → som → com) against the Python
catalog/com/subscription/crm/payment. **18/19 hero scenarios green** on the mixed
stack; the 1 failure is a pre-existing Python-portal branding assertion (see
below). 138 unit/integration tests (+42 over P1); fmt + clippy `-D warnings` clean.

### Done

- **`rust/services/mediation`** — TMF635 online mediation. Block-at-edge ingress:
  cheap policies → Subscription enrichment (`SubscriptionClient.get_by_msisdn`) →
  post-enrich policies → persist `usage_event` + inline-publish `usage.recorded`.
  Rejections leave **no** row, only a `usage.rejected` audit trace. First
  service-owned table write of the port. Live smoke proves the rejection path
  in-network + a `usage.rejected` row against live Subscription.
- **`rust/services/provisioning-sim`** — HLR/PCRF/OCS/SM-DP+ stand-in. Consumer +
  fault-injecting worker (`fail_always`/`fail_first_attempt`/`slow`/`stuck`) +
  the eSIM SM-DP+ seam (`sim`/`onbglobal`/`esim_access` — `select_esim_provider`
  fail-fast). The stateful retry loop mutates an in-memory task and persists once
  at the terminal state (externally identical to the Python flush-then-commit).
  Live smoke: worker completes `HLR_PROVISION` → `provisioning.task.completed`;
  deployed container drains the live `provisioning.task.created` queue.
- **`rust/services/som`** — the event-plane heart. Decomposes `order.in_progress`
  → ServiceOrder → CFS → RFS(Data,Voice) + atomic MSISDN/eSIM reservation
  (`InventoryClient`), drives `provisioning.task.*` to `service_order.completed`.
  Runs the **outbox relay** (its staged events' only publisher) and **four safe
  consumers**. Live smoke: HTTP surface + the relay drains a staged row to
  published against the live broker.

- **Platform crates grown (the deferred P0/P1 bindings, now validated):**
  - **`bss-events::start_relay` / `Relay` / `drain_once`** — the lapin/sqlx tick
    loop over the P0 `drain_batch` core: `FOR UPDATE SKIP LOCKED` drain →
    publish-with-`message_id` → mark, at-least-once. **som/com/subscription run
    it; the rest inline-publish.**
  - **`bss-events::bind_consumer` + `EventHandler`** — the safe consumer: declares
    the main/retry/parked topology (arg types matched aio-pika so the durable
    queues are shared byte-identically), inbox-dedups on `message_id`, runs the
    handler on the consumer's transaction, retries (TTL dead-letter) or parks. It
    processes deliveries **serially** — see the concurrency note below.
  - **`bss-events::MqChannel`** grew `publish_json_with_id`/`publish_bytes_with_id`,
    `declare_retry_exchange`, `bind_safe_consumer`, `publish_parked`.
  - **`bss-clients::{SubscriptionClient, InventoryClient}`** — the two typed
    clients this phase needs (by-msisdn lookup; reserve/release MSISDN + eSIM).
  - **`bss-admin` (new crate)** — the shared `admin_reset_router` (operational-data
    wipe, `BSS_ALLOW_ADMIN_RESET`-gated). Ported here because the Phase-2 scenarios
    call each service's `/reset-operational-data`. All three services mount it.
  - **`bss-clock::isoformat`** — Python `datetime.isoformat()` parity (micros, no
    fraction when zero, `+00:00`). The first R1 datetime-in-payload seam.

### Cutover into the running stack (per Decision D8)

All three run their Rust image via `docker-compose.rust.yml`
(`bss-{mediation,provisioning-sim,som}:rust`). Each verified in-network through the
deployed container (mediation reached `subscription:8000`; provisioning-sim drained
a published `task.created` → `completed` published_to_mq=true; som's 4 consumers +
relay started clean). The overlay ledger now reads rating + all three.

### The P1 order→provisioning "stall" — it was a misrun, not a bug

P1 deferred the full hero suite because `customer_signup_and_exhaust` stalled at
"wait for order to complete" (`order.stuck`). **The real cause was the P1 run
itself** — no `make scenarios-hero` provider-flip wrapper (payment still Stripe →
the charge never approved → no activation) + empty seed. Proof: the full
`scenarios-hero` suite passes on the **pure Python** event plane (verified — the
first P2 run tested Python som/prov before I noticed they'd been reverted, see the
deployment gotcha), and the Rust event plane passes the same scenarios (verified —
below). It was never a code stall.

**Separately**, while porting SOM I found a *real latent* concurrency bug in the
oracle: `handle_task_completed` does a read-modify-write on the CFS `characteristics`
JSONB (`pendingTasks[t]=completed`) with **no row lock**, and the Python aio-pika
consumer runs its callbacks **concurrently** (prefetch 5) — four simultaneous
`provisioning.task.completed` events *can* lose a `pendingTasks` update. It doesn't
manifest in the hero run (the four provisioning tasks have staggered durations, so
the completions arrive spaced out), but it's a genuine race. The Rust port hardens
it: the safe consumer processes deliveries serially and the handlers read the CFS
`FOR UPDATE`. **Noted for a Python backport** — a correctness improvement, not the
P1-stall fix.

### Exit criteria — met (validated against the confirmed Rust event plane)

Six event-plane hero scenarios run **directly** (`bss scenario run <file>`) with the
four Rust containers confirmed deployed throughout (payment flipped to mock; the
overlay held so som/provisioning-sim stayed Rust):

- `new_activation_with_provisioning_retry` ✅ (provisioning-retry-resilience — order
  completes *despite* the injected HLR fault; the retry path runs through Rust
  provisioning-sim + som) and `inventory_low_watermark_and_replenishment` ✅ — the
  two named exit criteria.
- `customer_signup_and_exhaust` ✅ 13/13, `trace_customer_signup_swimlane` ✅ (order
  completes in ~2.6s), `customer_buys_roaming_and_uses_it` ✅ (mediation roaming
  path), `customer_renews_automatically` ✅.
- Retry path exercised by the retry scenario; park-after-max is unit-pinned
  (`decide_retry`) and the topology declares the parked queue.

### Deployment gotcha (important for P3+ and P8)

`make scenarios-hero` recreates `portal-self-serve` (email-provider flip) with the
**base** compose file. `portal-self-serve` has a health-gated `depends_on:
[som, provisioning-sim, …]`, so compose reconciles those deps against the base spec
and **reverts the Rust som/provisioning-sim containers to Python** — because the
distroless Rust images carry **no `HEALTHCHECK`** (that's the Phase-8 "healthchecks
without curl" task). So `make scenarios-hero` as-is silently tests the Python event
plane. Until the Rust images get a healthcheck, validate with **`COMPOSE_FILE=docker-compose.yml:docker-compose.rust.yml`** exported (so every wrapper `docker compose`
keeps the overlay), or run the api-tagged event-plane scenarios directly with the
overlay held (what was done here). The 4 portal-tagged hero scenarios still need the
portal and are out of scope for the Rust event-plane validation.

### Bugs caught by the deployed cutover (playbook §7)

- **`NOT_ALLOWED - attempt to reuse consumer tag 'som'`** — all four SOM consumers
  shared one consumer tag on one connection; RabbitMQ requires unique tags (aio-pika
  auto-generates them). Fixed: the (unique) queue name is the tag.
- **Nanosecond datetime drift** — mediation's `rejectedAt` serialized 9-digit
  nanoseconds vs Python's 6-digit micros. Fixed via `bss_clock::isoformat` (R1 seam).

---

## Phase 1 — Pilot: rating — ✅ COMPLETE (tag `v2.0.0-phase.1`)

The first Python service ported to Rust, and the **per-service porting playbook**
([`PLAYBOOK.md`](PLAYBOOK.md)) — the real Phase-1 deliverable — validated by
stamping it once. Proven end-to-end against the **live stack**: the Rust rating
service, as the sole consumer of `rating.usage.recorded`, turned a
`usage.recorded` into a `usage.rated` (audit row + published to MQ) via the live
Catalog and live Postgres. 96 unit/integration tests green (12 new for rating),
5 `#[ignore]` live-smoke checks green against the running stack; fmt + clippy
`-D warnings` clean.

### Done

- **`rust/services/rating`** (lib + bin) — port of `services/rating`:
  - **`domain.rs`** — pure `rate_usage` (over `serde_json::Value` tariff, faithful
    dict-shape reads) + `decide_usage_outcome` (the consumer's roaming-routing
    branch factored out as a pure fn so the full event-shape decision is CI-testable).
    12 unit tests port `test_rating_pure_function.py` + the payload assertions of
    `test_rating_event_consumer.py` 1:1 (error-substring matched for wire stability).
  - **HTTP** (`routes.rs` + `error.rs` + `lib.rs::create_app`) — `/health` (exempt)
    + `/ready` (token-required — only `/health*` is exempt, matching the oracle),
    `/rating-api/v1/{tariff/{id},rate-test}`, mounts `clock_admin_router` +
    `audit_events_router`. `ApiError` `IntoResponse` reproduces the ASGI middleware
    shapes (`RatingError`→422 `{code:"RATING_ERROR"}`, upstream 5xx→500, 404).
    axum-0.7 `:param` paths; token gate outermost, context inside.
  - **`consumer.rs`** — lapin consume loop on `usage.recorded`; inline-publish
    (rating runs **no** relay — only subscription/com/som do); publish-then-INSERT
    with resolved `published_to_mq`; consumer rows stamped from `RequestCtx::default()`
    (Python `auth_context` default). Acks unconditionally (handler owns its errors).
  - **`config.rs`** — `Settings::from_env()` (`BSS_<UPPER>`), sqlx DB-url normalize.
  - **`Dockerfile`** — multi-stage, distroless-cc final, non-root, port 8000.

- **Platform crates grown (reused by P2+):**
  - **`bss-clients::CatalogClient`** — first typed client (`get_offering`); thin
    wrapper over `BssClient`, only the call rating needs.
  - **`bss-events::audit_events_router(pool)`** — the shared `/audit-api/v1` read
    router (dynamic filters via `QueryBuilder`, camelCase out, ISO 422). Was
    deferred from P0; lands here where a service mounts it.
  - **`bss-events::MqChannel`** — lapin connect / declare `bss.events` topic
    exchange / `publish_json` (inline-publish parity, no `message_id`) /
    `declare_and_bind`. Runs lapin on the tokio runtime via the `tokio-*-trait`
    shims. **vhost fix:** an AMQP URL ending in bare `/` (empty vhost to lapin,
    default `/` to aio-pika) is normalized to `%2f`.
  - Workspace: `lapin` + `tokio-executor-trait`/`tokio-reactor-trait`/`futures-util`
    added; `bss-clients`/`bss-models` path deps + `services/*` member glob.

- **Live proof** (`services/rating/tests/live_smoke.rs`, `#[ignore]`, 4 checks) —
  the Phase-1 analogue of the P0 conformance harness, all **inert / cleaned up**:
  1. `CatalogClient` ↔ live Catalog + `rate_usage` on the **real** PLAN_M (caught
     the R1 shape: live PLAN_M carries `data_roaming`, `taxIncludedAmount.value`
     is a number, currency is `.unit`);
  2. full HTTP stack (health / authed tariff / 401 / rate-test / 422 / audit read)
     against live infra via in-process `axum::serve`;
  3. outbox INSERT + audit read-back for an inert aggregate, then `DELETE`;
  4. **consumer cutover** — `docker stop bss-cli-rating-1`, Rust binary drains the
     shared durable queue, publish one synthetic `usage.recorded` (non-existent
     sub → subscription catches-and-acks, no side effect), assert the Rust-written
     `usage.rated` (`published_to_mq=true`), clean up, `trap`-restart the container.

### Cutover into the running stack (per Decision D8, 2026-07-11)

Rating is **cut over in the running compose stack**, not just proven in isolation —
per the per-service cutover doctrine (D8: cut over at each phase, running stack;
oracle stays reproducible-on-demand).

- **Image + overlay:** `docker build -f rust/services/rating/Dockerfile -t
  bss-rating:rust rust/`; swapped in via `docker-compose.rust.yml`
  (`docker compose -f docker-compose.yml -f docker-compose.rust.yml up -d rating`).
  `bss-cli-rating-1` now runs `bss-rating:rust` (health reports version `1.8.1`,
  the Rust `BSS_RELEASE`, vs the Python image's `1.7.0`).
- **Mixed-stack functional proof:** published a real `usage.recorded` to the live
  `bss.events` exchange; the **deployed Rust container** consumed it, reached
  `catalog:8000` **over the compose network** (the real in-network path the host
  binary couldn't exercise), rated it, and wrote `usage.rated` (`published_to_mq=
  true`, `allowanceType=data`, `qty=250`, `charge=0`, `actor=system`,
  `service_identity=default`). Clean `INFO` logs; inert row cleaned up.
- **Bug caught at the deployed container (log review):** the tracing subscriber had
  no level filter → `lapin` logged at TRACE and **dumped the AMQP PLAIN handshake
  (broker password) into the logs**. Fixed in `bss_telemetry::init_telemetry`
  (default `info`; `lapin`/`amq_protocol*` pinned to `warn`; never default TRACE).
  Rebuilt + re-swapped; 0 leaky lines. This is exactly the class of error the
  per-service cutover is meant to surface early — logged in the playbook (§7).
- **Full hero suite (`make scenarios-hero`) not yet run — and why:** the running
  stack's operational data is currently empty (an `operational_data_reset`), and
  the full `customer_signup_and_exhaust` / `customer_buys_roaming_and_uses_it`
  scenarios need `make scenarios-hero`'s provider-flip wrapper (payment→mock,
  kyc→prebaked, email→logging + container recreation) plus a healthy order→
  provisioning path. A direct baseline run stuck at **order completion** —
  provisioning tasks all `completed`, but the som/com completion-event reaction
  didn't flip the order (`order.stuck`) — and it stuck the **same way on the pure
  Python stack** (pre-swap baseline), so it is a stack/data-state issue upstream of
  rating, not the port. Rating's own responsibility is validated by the mixed-stack
  event-path proof above; the full suite is a heavier, stack-reconfiguring step to
  run deliberately (with the wrapper + a seed) once the provisioning path is healthy.

### Deferred (by design, land where they're validated by real behaviour)

- The **relay tick loop** lapin/sqlx binding (drain→publish→mark) — only
  subscription/com/som run it, so it lands in P2/P3 against the real retry/park
  topology + the provisioning-retry hero scenario. The relay *core* (SQL, drain
  orchestration) already exists in `bss-events` from P0.
- The **compose image-swap** run of `make scenarios-hero` — the Dockerfile lands
  now; the container build + mixed-stack scenario sweep is the P8 image pass. The
  consumer cutover smoke already proves the runtime path against the live stack.
- Remaining `CatalogClient` surface (list/price/promotions/admin) — ports when
  Catalog itself lands (P3) or a consumer first needs a call.

### Notes / decisions taken

- **Local topology discovered:** the bss **app** containers run locally (published
  `localhost:8001`–`:8010`); the **infra** (Postgres/RabbitMQ/Jaeger) runs on the
  remote `tech-vm` over Tailscale. Point `BSS_CATALOG_URL=http://localhost:8001`
  for the live smoke; DB/MQ use the `.env` `tech-vm` URLs.
- **Consumer decision extracted as a pure fn** (`decide_usage_outcome`) is the
  reusable pattern — it moves the event-shape spec into CI without infra. Baked
  into the playbook.

---

## Phase 0 — Foundations — ✅ COMPLETE (tag `v2.0.0-phase.0`)

All exit criteria green against the live oracle via `cargo run -p conformance`
(2026-07-11): token-middleware conformance, an audit row the **Python** relay
publishes, a Rust-emitted trace in **Jaeger**, and golden HMAC vectors matching
the oracle. 8 platform crates + conformance harness; 84 unit tests + 5 live
checks; clippy `-D warnings` + fmt clean; CI wired.

Goal: Cargo workspace + CI + the seven platform crates against a throwaway
hello-world service (see `03-PHASES.md`).

### Done

- **Python baseline captured** → [`05-BASELINE.md`](05-BASELINE.md). The "before"
  measurement for motto #6, taken while the Python stack was live (it can't be
  reconstructed post-cutover). Headlines: **1.18 GiB** app-plane RAM (11
  containers), **6.36 s** full-stack cold start (all 11 booted together;
  per-service breakdown in the doc), **12.8 ms** p99 on `/health`, **~3.46 GB**
  nominal image sum, **109,297** LOC Python. Phase 8 re-measures the same way
  (§6 of that doc) and this is the comparison point.
- **Toolchain + scaffold.** rustup stable (1.97) with rustfmt + clippy. Cargo
  workspace at `rust/` (D7: subtree, not standalone repo — rationale in
  `rust/README.md`). Workspace lints: `unsafe_code = forbid`,
  clippy `unwrap_used`/`expect_used = warn` (promoted to deny by `-D warnings`).
- **CI from day one.** `.github/workflows/rust.yml` — fmt + clippy `-D warnings`
  + test on `2.0` pushes / PRs touching `rust/**`. (Closes the "no CI anywhere"
  gap the inventory flagged; sqlx-prepare job added when `bss-db` lands.)
- **`bss-clock`** (first crate — "everything reads it"). Faithful port of
  `packages/bss-clock`:
  - `now/freeze/unfreeze/advance/state/parse_duration/reset_for_tests`, wall &
    frozen modes. Process-global state via `ArcSwap<Inner>` with `rcu` writers
    (§2.2 of `02-TECH-MAPPING.md`) → lock-free `now()` reads.
  - `clock_admin_router()` (axum) mirrors the FastAPI router: `GET /clock/now`
    unguarded; `POST freeze|unfreeze|advance` gated on `BSS_ALLOW_ADMIN_RESET`;
    camelCase wire shape (`offsetSeconds`/`frozenAt`), RFC-3339 instants,
    `{"detail":{code,message}}` errors, 403/422 parity.
  - 15 integration tests porting `tests/test_clock.py` 1:1 (serialized on a
    process-global `Mutex` since the clock is a singleton). All green; fmt +
    clippy clean.

- **`bss-context`** — the §2.1 ContextVar translation. Unifies the Python
  per-service `auth_context.AuthContext` **and** `bss_clients.base` context vars
  into one `RequestCtx` (actor/tenant/channel/service_identity/request_id + roles/
  permissions, defaults matching the dataclass). Carried explicitly in axum
  extensions (`Extension<RequestCtx>`) *and* mirrored into a `tokio::task_local!`
  scope for the two chokepoint readers (bss-clients, bss-events) — the task-local
  lives only in this crate (future doctrine guard). `propagate_context` middleware
  ports `RequestIdMiddleware` (header→ctx, echo `x-request-id`); `service_identity`
  comes from a `ServiceIdentity` extension the token layer will set, never a header
  (guard #6 made structural). 10 tests (ports `test_auth_context.py` +
  `test_header_propagation.py` intent + task isolation); fmt + clippy clean.
  - Deferred: the `set_service_identity_token` per-call override becomes an explicit
    field on the orchestrator tool-context in P5 (§2.1), not a task-local — noted so
    bss-clients doesn't reach for one.

- **`bss-middleware`** — perimeter `X-BSS-API-Token` auth (risk R4). `TokenMap`
  (HMAC-SHA-256 via `hmac`+`sha2`, constant-time full-scan lookup via `subtle`,
  env-name→identity derivation), loader + validator (default-required, unique
  identities/tokens, sentinel/length), and the axum `require_api_token` gate
  (`/health*` + `/webhooks/` exemptions, 401 shapes). Wires to bss-context: the
  gate inserts `ServiceIdentity` (guard #6 — identity from the token only, never a
  header), the context layer reads it — proven by a composed layer test.
  - **Golden-vector conformance**: captured HMAC digests + identity derivations
    from the live Python oracle → `tests/golden_vectors.json`; two Rust tests
    assert byte-identical hashing/derivation. This is the R4 mitigation.
  - 28 tests (port `test_api_token.py` + `test_token_auth.py` + golden). Deferred:
    the per-`(remote,path)` 401 log throttle (observability; lands with bss-telemetry).

- **`bss-db`** — the `PolicyViolation` type (the single most load-bearing payload;
  the LLM reads it) + sqlx pool. Ports `policies/base.PolicyViolation` (raise side,
  field `rule`), the `RequestIdMiddleware` 422 serialization (wire side: `rule`→
  `reason` + derived `referenceError`, five keys exactly), and the client parse
  (`bss_clients.base._handle_response`) as `from_wire`. `IntoResponse` makes the
  422 contract compiler-enforced. sqlx `PgPool` with the SQLAlchemy 5+5 config
  (`connect`). 7 tests pin the exact wire shape + server→client round-trip.
  - Deferred: a live-captured golden 422 from the running stack can augment the
    hand-pinned shape once the conformance service exists.
- **`bss-models`** (started) — `BSS_RELEASE` single source of truth (guard #14),
  tracking the Python baseline `1.8.1`. The ~60 per-table `FromRow` structs are
  intentionally deferred: each ports **with its service** (P1–P4) against that
  service's golden contract tests, where the R1 dict-shape hazards concentrate.

- **`bss-clients`** (base done) — the reqwest S2S base. Ports `BSSClient`:
  mandatory per-request timeouts, **no retries**, typed `ClientError` (404→NotFound,
  422+POLICY_VIOLATION→`Policy(bss_db::PolicyViolation)` reusing that type, other
  422/4xx→Http, 5xx→Server, timeout, transport). `AuthProvider` trait +
  No/Token/Bearer/NamedToken (fail-fast constructors; NamedToken primary→fallback
  env). Context propagation reads `bss_context::current().outbound_headers()` with
  set-default semantics — **no `set_context`**, the §2.1 unification pays off. 11
  tests run the real reqwest path against a local axum peer (respx equivalent):
  error mapping, no-retry (call-count=1), per-call timeout, auth+ctx headers.
  - Deferred: the 12 typed clients (CRMClient, …) port per-phase (P1–P4); the
    per-call service-identity token override lands with the orchestrator (P5, §2.1).

- **`bss-telemetry`** (rules done) — the two pure, load-bearing pieces: the
  log-field **redaction** rules (`REDACTED_KEYS` minus `_ref`/`_id` suffixes →
  `***REDACTED***`, top-level keys only, no recursion — ports `redact_sensitive`)
  and the **semconv** span attribute keys (`bss.*`, last4-only discipline). 4 tests.
  - Deferred to the conformance-service step: the tracing-subscriber JSON setup,
    the OTLP/OTel exporter, and the tracing `Layer` that applies `redact_event` to
    live events (validated against Jaeger there) — "instrument at the chokepoint".

- **`bss-events`** (core done) — the transactional-outbox plane, broker-free core:
  - `stage_event` builds the `audit.domain_event` row stamped from `RequestCtx` +
    `bss_clock::now()` (ports `events/publisher.publish`); `published_to_mq=false`.
  - `drain_batch` — the relay orchestration (publish→mark, at-least-once, null
    payload→`{}`) over an `EventPublisher` trait; tested against a fake. The
    `DRAIN_SQL`/`MARK_OK_SQL`/`MARK_FAIL_SQL` are verbatim (SKIP LOCKED, oldest
    first). `relay_mode(None)=Off` (delivery off, log still records).
  - `decide_retry` (park at `>= max_retries`, else nack-retry) + `death_count`
    (`x-death[0].count`) — the safe-consumer decision, plus `CLAIM_INBOX_SQL`.
  - `topology` — the frozen RabbitMQ contract as assertable data (exchange names,
    main/retry queue args, parked/retry names) so a Rust and a Python service share
    a broker byte-identically during migration.
  - 8 tests (port `test_relay.py` + `test_consumer.py` intent + contract pins).
  - Deferred to conformance: lapin connect/declare/consume, the sqlx tick loop, and
    the `/audit-api/v1` read router (needs Postgres+RabbitMQ to validate).

- **`conformance` harness** (`rust/conformance`, `cargo run -p conformance`) — the
  Phase-0 exit harness, run against the **live stack** (Postgres/RabbitMQ on
  `tech-vm`, the same infra the Python services use; reachable from the dev host
  over Tailscale). Never runs in CI. **All checks green (2026-07-11):**
  - sqlx connects to the live Postgres (16.14).
  - `audit.domain_event` schema matches `bss_events::DomainEvent` (16/16 columns).
  - **cross-language outbox interop: the *Python* relay published a Rust-written
    audit row** — INSERT an inert `conformance.ping` (no consumer bound), poll until
    `published_to_mq` flips, then DELETE. Zero side effects.
  - token middleware end-to-end over real HTTP with the live `BSS_API_TOKEN`
    (health 200 / no-token 401 / valid-token 200, identity=`default`).
  - Component model confirmed for the human: sqlx/lapin/reqwest/otel are libraries
    compiled into the binary — **no new infra, nothing to deploy**; Rust reuses the
    existing Postgres/RabbitMQ/Jaeger. (D-note in `rust/README.md`.)

- **`bss-telemetry` OTel bootstrap** — `init_telemetry(service)` builds a
  `TracerProvider` with an OTLP/HTTP-protobuf exporter to the same Jaeger the
  Python stack uses (`service.name = bss-<service>`, `TraceIdRatioBased` sampler,
  batch export), bridges `tracing` spans via tracing-opentelemetry, adds a JSON
  log layer, and never panics (falls back to logs-only). `TelemetryGuard` flushes
  on drop. `emit_probe_span` returns a trace id for the Jaeger conformance check.
  opentelemetry 0.27.x pinned (R6 version-matrix resolved cleanly).
  - One follow-up: the redaction **Layer** over live `tracing` fields (the rules +
    `redact_event` exist; wiring them as a fmt field-visitor lands when the first
    service logs sensitive fields — no service does yet).

### Phase 0 done → Phase 1 (rating pilot)

Tagged `v2.0.0-phase.0`. Next: **Phase 1 — port the rating service** (smallest,
"rating is a pure function"), the pilot that turns the platform crates into a
running Rust service and produces the per-service porting playbook. This is where
the per-endpoint golden-contract capture rig gets fleshed out (capture rating's
request/response/event JSON from the Python oracle, diff the Rust service against
it), and where bss-clients' first typed client (catalog) + the lapin/sqlx service
wiring (relay tick loop, consumer, `/audit-api/v1` router) land.

### Notes / decisions taken

- **Deps pinned minimal:** chrono, arc-swap, serde_json, axum (+ tokio/tower dev).
  No `regex` — `parse_duration` is hand-rolled to match `^\s*(\d+)\s*([smhd])\s*$`
  without the dependency.
- Clock tests need `--test-threads` safety: solved in-crate with a serialising
  `Mutex` + `reset_for_tests()`, not by constraining the runner.
