# Roadmap: Open Order + MSISDN Reservation

> **Status:** Proposed (post-2.0). Design locked, ready to slice — not yet scheduled.
> **Owner:** operator. **Scope:** self-serve signup funnel + inventory + a sweep worker.
> **Motto/doctrine impact:** none to the seven principles. Touches `DATA_MODEL.md`
> and `TOOL_SURFACE.md` at build time (see [§9](#9-protected-doc-impact)).

## 1. Problem

The signup funnel is *incidentally* resumable but not *safely* so:

- **No real reservation.** Picking an MSISDN does **not** hold it. The number stays
  `available` in `inventory.msisdn_pool` until SOM reserves it at **provisioning** time
  (near the end of the funnel). Between "pick number" and "provision," two *different*
  customers can select the same number — the second only fails late. It *appears* to work
  only because re-entering as the **same** identity reconciles against server state.
- **The open order is ephemeral.** The signup "session" lives in a per-process
  `Mutex<HashMap>` (`portals/self-serve/src/signup_session.rs`) — not persisted, not
  per-customer queryable, gone on restart. Nothing to *show* and nothing to *expire*.
- **No expiry.** Fine today (nothing is held), but the moment reservation is real, an
  abandoned funnel leaks the number without a release path.

## 2. Goals

1. **Reserve on number-pick.** The instant a customer picks an MSISDN, it is held so no one
   else can select it.
2. **"My open order"** under My Account — a low-prominence screen to resume (or cancel) a
   pending order.
3. **24-hour auto-expiry** — an open order not completed in 24h expires; its number is
   released back to the pool.

## 3. Decisions (locked)

| # | Decision |
|---|---|
| 1 | **Widen the pool table** — add reservation columns to `inventory.msisdn_pool` (not a separate table). |
| 2 | **Dedicated `open_order` aggregate** — do *not* overload the COM `ProductOrder`. |
| 3 | **One open order per customer.** A customer with an open order is **blocked from starting another** until they **complete or cancel** it. (Cancel releases the number so they can re-order.) |
| 4 | **Reserve at number-pick** (earliest point — kills the collision window). |

**Constraints:** one order = one line; multiple *completed* subscriptions per customer are
allowed; reservation-hold (24h, releasable) is distinct from ported-out `quarantine_until`
(terminal — v0.17). Not a cart, not partial payment.

## 4. Current state (grounded)

| Piece | Today |
|---|---|
| MSISDN pool | `inventory.msisdn_pool` — `status` (default `available`), `reserved_at`, `assigned_to_subscription_id`. **No TTL, no reserved-for.** |
| eSIM pool | `inventory.esim_profile` — FSM `available→reserved→…`, reserved at **provisioning**, not pick. |
| Signup order | in-memory `SignupStore` (`Mutex<HashMap>`), per-process, no persistence/TTL. |
| Reserve point | provisioning (funnel end) — that's the collision window. |
| Row structs | `MsisdnRow` / `EsimRow` in `services/crm/src/repo.rs`, name-based `FromRow`. |

## 5. Design

The reservation + open order sit **in front of** the existing COM order creation — that stays
at the funnel end, unchanged.

### 5.1 Reservation — MSISDN only, at pick (inventory / `crm` service)

- Widen `inventory.msisdn_pool`: **`reserved_until timestamptz NULL`**, **`reserved_for text
  NULL`** (the `open_order` id). Reuse the existing `available→reserved→available` FSM — no
  new pool states.
- **Reserve at pick is a single atomic, self-healing UPDATE** (no read-then-write TOCTOU):
  ```sql
  UPDATE inventory.msisdn_pool
     SET status='reserved', reserved_at=$now, reserved_until=$now_plus_24h, reserved_for=$oo
   WHERE msisdn=$1
     AND (status='available' OR (status='reserved' AND reserved_until < $now))
  RETURNING msisdn;
  ```
  0 rows → "that number was just taken, pick another." The `reserved_until < now` clause makes
  it **self-healing**: an expired-but-not-yet-swept hold is reclaimable immediately, so the
  picker never wedges on lag.
- **Picker + available-count queries** exclude live holds: `status='available' OR (status
  ='reserved' AND reserved_until < now())`.
- **eSIM is *not* reserved at pick.** Provisioning is the funnel's *last* step, so an abandoned
  order (the expiry case) never reserved an eSIM — only the MSISDN needs releasing. eSIM keeps
  its current provisioning-time lifecycle. (Defensive: the release op releases the eSIM too
  *if* one was ever reserved for the order — cheap idempotent no-op otherwise.)

### 5.2 `open_order` aggregate (persisted, resumable)

- New table (schema `inventory` or a new `order_mgmt` sibling — [§8](#8-implementation-notes)):
  `id`, `tenant_id`, `owner_identity` (email — known at pick), `customer_id` (**nullable**,
  filled at the create-customer step), `plan_code` (+ price snapshot), `msisdn`, `iccid`
  (null until provisioning), `step`, `status` (`open|completed|cancelled|expired`),
  `reserved_until`, `created_at`, `updated_at`.
- **Created at number-pick**, keyed on `owner_identity` (the customer record doesn't exist yet;
  the verified email does). `customer_id` is linked when create-customer runs.
- **One-open-per-owner** enforced by a **partial unique index**:
  `UNIQUE (owner_identity) WHERE status='open'`. Starting a second funnel while one is open →
  **blocked** with a message pointing at "My open order" (complete or cancel).
- **State machine:** `open → completed` (funnel done), `open → cancelled` (customer), `open →
  expired` (sweep). Cancel + expire both call `inventory.release`.
- `SignupStore` becomes a **write-through cache** over this row (or is replaced). Resume = load
  the owner's `open` row and re-enter the funnel at `step`.

### 5.3 "My open order" screen (self-serve portal)

- `GET /account/open-order`, linked from **My Account** (alongside `/profile/*`,
  `/payment-methods`, `/billing/history`) — **not** the dashboard hero. Section-degrading:
  absent when there's no open order.
- **Ownership-bound (`*.mine`, v0.12):** owner comes from `request.state` (session identity),
  never the request. Cross-owner → 403.
- Shows plan, held number, **"reserved until \<time\>"**; CTAs **Resume** (→ funnel at `step`)
  and **Cancel** (→ release + `cancelled`, frees them to re-order).

### 5.4 Expiry sweep worker (no cron — mirror the renewal worker)

- A **lifespan tick loop** in the `crm` service (it owns inventory) — same discipline as the
  subscription **renewal worker** (`services/subscription/src/repo.rs`, `FOR UPDATE SKIP
  LOCKED`, no cron/Celery). *(New: crm has no worker today — this adds one to its lifespan.)*
- Each tick: `open_order WHERE status='open' AND reserved_until <= clock.now()`, lock-skip;
  per row → `inventory.release(order_id)` (MSISDN → `available`, eSIM too if held), `open_order
  → expired`, emit event. All policy-gated.
- **`bss_clock::now()`** for the TTL and the sweep (deterministic; grep-guarded) — never wall
  clock. Makes "advance 24h → number releases" a deterministic scenario test.

### 5.5 Events (first-class → `audit.domain_event`)

`inventory.msisdn.reserved`, `inventory.msisdn.released`, `open_order.opened`,
`open_order.completed`, `open_order.cancelled`, `open_order.expired`. Same tx as the write;
outbox relays post-commit.

## 6. Impact assessment (the "hidden" ripple — the part that isn't simple)

### 6.1 Migration
- **Add `migrations/0002_open_order_reservation.sql`** (ALTER the pool table + CREATE
  `open_order` + the partial unique index). The sqlx `MIGRATOR` (`crates/bss-db/src/migrate.rs`)
  applies `000N_*` siblings in order; existing DBs have `0001` **stamped** (not run), so
  `bss admin migrate` runs *only* `0002`. Fresh installs run both. ✅ Path exists.
- **New columns are `NULL`-able with safe defaults** → existing seeded rows need no backfill.
- ⚠️ **`reset.rs` (`crates/bss-admin/src/reset.rs`)** — the `open_order` table must be added to
  the reset `TableReset` plan, or `bss admin reset` leaves stale open orders behind. **Audit the
  reset plan.**

### 6.2 Seeding
- ✅ **Low impact.** `cli/src/commands/admin_seed.rs` seeds with **explicit column lists**
  (`INSERT INTO inventory.msisdn_pool (msisdn, status) … ON CONFLICT DO NOTHING`), so new
  nullable columns are simply omitted and default correctly. **No seed change required** — but
  the seed *may* optionally set `reserved_until=NULL` explicitly for clarity.
- `open_order` seeds **empty** (it's runtime state, not reference data).

### 6.3 Row structs / queries
- ✅ **`MsisdnRow` / `EsimRow` are name-based `FromRow`** → adding table columns does **not**
  break existing `query_as` reads (extra columns are ignored). Add `reserved_until` /
  `reserved_for` to `MsisdnRow` **only** where read.
- ⚠️ **Audit for column-less `INSERT INTO … VALUES`** (positional) anywhere against the pool
  tables — those break on a widened table. (Seed is safe; check `service.rs` reserve/assign
  writes — they should be targeted `UPDATE`s, but confirm.)

### 6.4 API / behaviour
- **MSISDN picker + available-count** endpoints change to exclude live holds (§5.1). This is a
  read-contract change every "pick a number" caller sees — verify the CLI/scenario callers too.
- **New endpoints:** `inventory.reserve` / `inventory.release`; `open_order` get/cancel
  (customer-side `*.mine`). New `bss-clients` methods.
- **TMF payloads:** `reserved_until`/`reserved_for` stay **internal** — do **not** add them to
  the TMF620/638 read payloads (the schema builders in `services/crm/src/schemas.rs` emit
  `reserved_at`; leave the TMF surface spec-clean). Grep the schema builders to confirm no
  accidental `SELECT *`-to-payload leak.
- **Funnel reorder:** the `open_order` row + reservation now happen at **pick**, earlier than
  any current write. The create-customer step must **link** `customer_id` onto the existing
  `open_order` rather than assume none exists. Resume + the one-open-per-owner block are new
  branch points in `signup.rs`.
- **Ownership:** the `*.mine` open-order read needs an `OWNERSHIP_PATHS`/profile entry
  (`customer_self_serve`) — security review checklist applies.

### 6.5 Concurrency / correctness
- Reserve is the **atomic self-healing UPDATE** (§5.1) — no read-then-write race.
- The sweep and a late completion can race (customer completes at T+24h as the sweep fires):
  the completion path must **re-check `reserved_until`/`status` under a row lock** and fail
  gracefully ("your reservation expired, please re-pick") rather than provisioning a released
  number.

## 7. Touchpoints
`services/crm` (inventory reserve/release + `open_order` aggregate/repo/policies + sweep
worker) · `crates/bss-clients` (reserve/release, open-order reads) · `portals/self-serve`
(pick reserves; picker excludes holds; `/account/open-order`; funnel link + block/resume) ·
`migrations/0002_*` · `crates/bss-admin/reset.rs` (reset plan) · scenario test
(reserve → abandon → clock+24h → released).

## 8. Implementation notes
- **`open_order` schema home:** `inventory` (co-located with the pool it reserves) is simplest;
  `order_mgmt` is cleaner-by-domain. Lean `inventory` unless it grows order-ish fields.
- **Owner key:** `owner_identity` (verified email) at pick → link `customer_id` at
  create-customer. The partial unique index is on `owner_identity`.

## 9. Protected-doc impact
Requires **Phase 0 amendments** at build time (do not edit without one):
- **`DATA_MODEL.md`** — `reserved_until`/`reserved_for` on `inventory.msisdn_pool`; the
  `open_order` table + its FSM + the partial unique index.
- **`TOOL_SURFACE.md`** — `inventory.reserve`/`release`, `open_order.get.mine` /
  `open_order.cancel.mine`, with `customer_self_serve` ownership entries.
- `CLAUDE.md` — no change (within scope + motto).

## 10. Suggested phasing
1. ✅ **Reservation core** (`4c0ff31`) — `0002` migration (columns) + atomic self-healing
   hold/release + events + reset-plan update.
2. ✅ **Persisted open order + reserve-at-pick** (`053cf60`) — `0003` `open_order` table +
   funnel write-through (create/hold at submit, link, complete) + one-open block +
   resume-by-load + the `reserve_next_msisdn` soft→hard *claim* so a held number flows
   through provisioning. *(Collisions now stop for a live signup.)*
3. ✅ **Account screen** — `GET /account/open-order` (ownership-bound: keyed on the
   session email, cancel derives the id from the session) + `POST …/cancel` (releases the
   hold) + a dashboard "Resume open order" link shown only when one exists +
   `open_order.html`. Section-degrading.
4. ⬜ **Sweep worker** — 24h release in the crm lifespan + deterministic-clock scenario test.

Ship 1–2 first (correctness — done), then 3–4 (UX + hygiene).
