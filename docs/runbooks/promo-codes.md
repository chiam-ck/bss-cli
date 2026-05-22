# Running promo codes (v1.1)

> **Audience:** operators running BSS-CLI v1.1+. v1.1 promos are **loyalty
> entitlements** — a different mechanism from the v0.7 windowed-price promos in
> `cny-promo.md`. Use this runbook for typed codes ("enter SUMMER25 at checkout")
> and targeted offers ("give these 20 customers 20% off"). For a plain time-boxed
> price drop on an existing plan, the v0.7 windowed price is simpler — see
> `cny-promo.md`.

## How it works (the two-system split)

BSS-CLI does not contain a promotions engine. The separate **loyalty-cli**
service is the entitlement brain (does this customer have a claim, is there
inventory, was it used). BSS owns only the **money terms** — a `catalog.promotion`
row — and the join key to a loyalty *OfferDefinition*. The two compose over HTTP;
loyalty ships unmodified.

- **Non-targeted** = a shared/multi-use *code* the customer types at checkout.
- **Targeted** = a *codeless* offer an operator pre-assigns to chosen customers;
  it auto-applies at their next order and shows on their dashboard.

The discount **composes** with the catalog's lowest-active price (unlike v0.7
windowed prices, which don't stack): the base snapshot is selected first, then
the promo discount applies on top.

## Prerequisites — env

The catalog and COM services each hold a loyalty client; the token never leaves
those processes. Both fail fast at boot if it's missing.

```bash
BSS_LOYALTY_BASE_URL=http://<loyalty-host>:8080   # loyalty-cli HTTP root
BSS_LOYALTY_API_TOKEN=<loyalty-cli's LOYALTY_API_TOKEN>
```

In the bundled compose these flow to every service via `env_file: .env`. Verify
loyalty is reachable: `curl -s $BSS_LOYALTY_BASE_URL/healthz` → `200`.

## Non-targeted: a typed code

```bash
bss promo create \
    --id PROMO_SUMMER25 \
    --type percent --value 20 \
    --duration multi --periods 3 \
    --code SUMMER25 --code-kind multi_use
```

`--duration`:

| kind | meaning | `--periods` |
|---|---|---|
| `single` | discount on the activation period only | omit |
| `multi` | discount for N billing periods, then full price | required, ≥ 2 |
| `perpetual` | discount never reverts | omit |

The customer enters `SUMMER25` on the signup form (or an order is placed with
`order.create --discount-code SUMMER25`). The live preview shows the discounted
price; an invalid code never blocks the order — it just proceeds at full price.

Restrict to specific plans with `--offerings PLAN_M,PLAN_L` (omit = all sellable).
Absolute discounts: `--type absolute --value 5.00`.

## Targeted: a codeless assigned offer

```bash
# 1. Create the promotion WITHOUT a code (codeless = targeted).
bss promo create --id PROMO_VIP --type percent --value 20 --duration single

# 2. Assign it to chosen customers (one loyalty offer.issue per customer).
bss promo assign --promo PROMO_VIP --customers CUST-001,CUST-007,CUST-042
```

`bss promo assign` is re-runnable: a customer who already holds the offer is
reported under `skipped`. For repeatable demo data, `seed_targeted_campaign.py`
at the repo root creates a promo and assigns it to a set of customers.

## Lifecycle — claim at activation

The entitlement is **consumed at activation, not at order create** (so a
provisioning failure never burns a single-use code):

1. `order.create` validates + stamps the discount as *intent* on the order item.
2. SOM provisions (a failure here costs nothing — nothing claimed yet).
3. On `service_order.completed`, COM **claims** the entitlement (the gate),
   subscription charges the **effective** price for period 1, then COM
   **redeems**. If the activation charge declines, COM **revokes** the
   entitlement and the order fails.

Renewal decrements a per-subscription counter: discounted while it's live, then
full price. A **plan change ends the promo** (the discount fields clear at the
pivot). Perpetual never decrements.

## Verifying

```bash
bss promo show PROMO_SUMMER25            # state=active, offerDefinitionId set
bss order create --customer CUST-007 --offering PLAN_M --discount-code SUMMER25
bss subscription show SUB-NNN            # priceAmount = full base; effectiveAmount = discounted
                                         # discountPeriodsRemaining counts down on renewal
```

The dashboard shows the applied discount on the line card and any unused
targeted offer ("🎁 You have a 20%-off offer").

## Troubleshooting

- **`catalog.promotion.loyalty_refused`** — loyalty rejected the registration
  (e.g. duplicate code). The promotion row stays `pending_link` (harmless — no
  live entitlement points at it). Re-run `bss promo create` with the same `--id`
  to resume the saga (loyalty calls are idempotency-keyed on the promotion id).
- **Boot fails: `BSS_LOYALTY_API_TOKEN is unset`** — set the env (above) and
  restart catalog + COM.
- **401 from loyalty** — token mismatch between BSS and loyalty-cli's
  `LOYALTY_API_TOKEN`.
- **Preview shows "isn't valid" for a real code** — check `bss promo show` state
  is `active` (not `pending_link`), the code applies to that offering
  (`--offerings`), and the validity window is open.
- **Targeted offer didn't apply** — confirm the offer is `issued` to that
  customer (re-run `bss promo assign`; it's idempotent) and the promotion is
  active.

## Anti-patterns

- Don't expose `promo.create` / `promo.assign` to the customer chat surface — they
  are operator-only by doctrine. A customer types a code; they never issue one.
- Don't hand-edit `catalog.promotion` rows. The OfferDefinition link is owned by
  the create saga; a manual row with no loyalty OD is dead weight.
- Don't combine a typed code and a targeted offer expecting them to stack — order
  resolution applies one discount (typed code takes precedence; otherwise the best
  applicable assigned offer).
