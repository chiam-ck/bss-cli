# Migration Progress Log

Running log of the phases/2.0 Rust port. One entry per work session. The plan
docs (`00`–`04`) are the *design*; this file is the *state*.

Branch: `2.0`. Workspace: [`../../rust/`](../../rust/).

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

## Phase 5 — orchestrator lib + knowledge + cockpit-core — 🚧 IN PROGRESS

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

### Phase 5c — bss-orchestrator (slices 1–14) — 🚧 (2026-07-14)

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
