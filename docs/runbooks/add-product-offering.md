# Adding a new product offering

> **Audience:** operators with admin access running BSS-CLI v0.7+. Adds a new tier (e.g. PLAN_XS, PLAN_XL, PLAN_M_ROAM) without a code change.
>
> **See also:** [`docs/HANDBOOK.md` §8.1](../HANDBOOK.md#81-catalog--add-an-offering-with-roaming) for the full catalog runbook in context. [`docs/runbooks/cny-promo.md`](cny-promo.md) for windowed-offering promo patterns. [`docs/runbooks/migrating-customers-to-new-price.md`](migrating-customers-to-new-price.md) for moving existing subscribers.

## Three ways in

| Surface | When to use it |
|---|---|
| `bss admin catalog *` (this runbook) | Scripted / repeatable changes, exact flags, CI. |
| Cockpit chat — REPL `bss` or `localhost:9002/cockpit/<id>` (v2.1+) | Conversational. Ask for what you want; the agent asks back for anything you left out, then stages the write for `/confirm`. |
| Cockpit browser → **Catalog** screen (v1.6.1+) | Point-and-click forms; retire is checkbox-confirmed. |

All three go through the same policy-gated `admin_*` catalog surface — there is no raw-CRUD path.

### The chat path (v2.1+)

`catalog.add_offering`, `catalog.add_price`, `catalog.window_offering`, and
`catalog.retire_offering` are on the `operator_cockpit` tool surface. They are
**`/confirm`-gated**, exactly like `subscription.terminate`: the agent's call is
staged as a proposal, you read the args, you type `/confirm`, and the next turn
executes it.

The agent will **not** fill in blanks for you. Ask for "a new 5GB plan" and it comes
back asking for the plan id, the price, and the allowances — by design, since a
guessed plan id or price is indistinguishable from an operator decision once it is in
the catalog. Supply them across as many turns as you like:

```
you › add a new plan, 5GB, no voice or SMS
bss › What plan id and monthly price should it have?
you › PLAN_XS, 9 dollars
bss › [staged] catalog.add_offering offering_id='PLAN_XS' name='Mini' amount='9.00' data_mb=5120
      Pending /confirm for catalog.add_offering
you › /confirm
```

Two notes carried by the tools themselves, worth knowing before you confirm:

- **`add_price` with `retire_current=false` is a promo, not a price change.** Both
  rows stay live and the *lowest* amount wins. `retire_current=true` closes the old
  rows so the new one takes over. Different customer outcomes — say which you mean.
- **Retiring is not cancelling.** `catalog.retire_offering` stops *new* orders;
  existing subscriptions keep renewing on their order-time price snapshot.

To **retire, decommission, withdraw, or remove** an offering, the verb is
`catalog.retire_offering` (CLI: `bss admin catalog retire-offering --id <PLAN>`).
There is no hard delete.

## Prerequisites

- `bss` CLI installed and pointed at the target deployment (`BSS_API_TOKEN`, service URLs in `.env`).
- A `--spec-id` value already in `catalog.product_specification`. The seeded `SPEC_MOBILE_PREPAID` covers v0.7+ needs.

## Procedure (data + voice + SMS)

```bash
# Add PLAN_XS — 5GB data, no voice, no SMS, no roaming.
bss admin catalog add-offering \
    --id PLAN_XS \
    --name "Mini" \
    --price 5.00 \
    --currency SGD \
    --data-mb 5120
```

The command writes one `product_offering` row, one `product_offering_price` row (`PRICE_PLAN_XS`), and one `bundle_allowance` row (data) through the catalog admin service. No raw SQL.

Optional flags:
- `--voice-min N` — adds a `voice` allowance (`-1` for unlimited).
- `--sms-count N` — adds an `sms` allowance (`-1` for unlimited).
- `--valid-from <iso>` / `--valid-to <iso>` — windows the offering at creation (see [Time-window an offering](#time-window-an-offering-at-creation) below).

## Verify

```bash
bss admin catalog show          # Active catalog at the current moment
bss catalog list                # Customer-facing read
```

## Roaming (v0.17+)

Roaming is a first-class allowance type alongside `data`, `voice`, `sms`. Two facts about roaming worth knowing before adding an offering:

1. **Roaming is additive.** A subscription's `is_exhausted` predicate considers only the *primary* allowance set (`data`, `voice`, `sms`). An exhausted `data_roaming` balance rejects roaming usage with rule `subscription.usage_rated.roaming_balance_required` but the subscription itself stays `active` (home data still works).
2. **A plan with zero included roaming can still accept a roaming top-up.** When the customer purchases `VAS_ROAMING_1GB`, the subscription synthesizes a fresh `data_roaming` `BundleBalance` row.

### Adding a roaming-included offering (v0.20+)

```bash
bss admin catalog add-offering \
    --id PLAN_XS_ROAM \
    --name "Mini + Roaming" \
    --price 8.00 \
    --currency SGD \
    --data-mb 5120 \
    --data-roaming-mb 1024
```

The command writes a fourth `bundle_allowance` row (`BA_<offering>_ROAM`, `allowance_type='data_roaming'`, `unit='mb'`) atomically alongside the data / voice / SMS rows. `--data-roaming-mb 0` is permitted and means "this plan has no included roaming, but the customer can still top up via `VAS_ROAMING_*`" — same as the seeded `PLAN_S`.

### Customers without included roaming can still top up

A customer on `PLAN_S` (which carries `data_roaming = 0 mb`) can still purchase `VAS_ROAMING_1GB`. The subscription's `purchase_vas` materializes a `data_roaming` `BundleBalance` row on demand. After exhaustion, roaming usage is rejected with `subscription.usage_rated.roaming_balance_required` while home data keeps working — see [`docs/HANDBOOK.md` §7.6](../HANDBOOK.md#76-roaming-v017).

> [!info] **(v0.17–v0.19 historical.)** Earlier releases lacked the `--data-roaming-mb` flag and required either a SQL `INSERT INTO catalog.bundle_allowance` or an edit + re-seed of `packages/bss-seed/bss_seed/catalog.py`. v0.20 closes that gap; the workaround is no longer needed. The seed module continues to use raw inserts for determinism — that's the seed's contract, not an operator path.

## Time-window an offering at creation

To launch a windowed offering (e.g. a CNY promotional plan that's only sellable during a date range):

```bash
bss admin catalog add-offering \
    --id PLAN_CNY \
    --name "Lunar New Year Promo" \
    --price 12.00 \
    --currency SGD \
    --valid-from 2026-02-10T00:00:00Z \
    --valid-to 2026-02-24T00:00:00Z \
    --data-mb 30720
```

After 2026-02-24, `bss catalog list` no longer surfaces `PLAN_CNY` for new orders. Customers who ordered during the window keep their snapshot price untouched — see [`docs/runbooks/migrating-customers-to-new-price.md`](migrating-customers-to-new-price.md) for explicit price moves.

## Verify with point-in-time queries

```bash
# What was sellable on 2026-02-15?
bss admin catalog show --at 2026-02-15T00:00:00Z

# Active right now (default).
bss admin catalog show
```

## Rollback

```bash
bss admin catalog window-offering --id PLAN_XS --valid-to <now>
```

Retires the offering immediately for new orders. Existing subscriptions on PLAN_XS keep renewing on their snapshot price. To migrate them off:
- Schedule a plan-change per customer via `subscription.schedule_plan_change`, OR
- Run `bss admin catalog migrate-price --offering PLAN_XS --new-price-id <PRICE-XX>` for a like-for-like price move (see [`migrating-customers-to-new-price.md`](migrating-customers-to-new-price.md)).
