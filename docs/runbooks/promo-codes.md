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
- **Targeted** = a *code* (in loyalty, but unadvertised) gated by a BSS
  eligibility list; it auto-applies at an eligible customer's next order and
  shows on their dashboard. A typed attempt by a non-eligible customer is rejected.

The discount **composes** with the catalog's lowest-active price (unlike v0.7
windowed prices, which don't stack): the base snapshot is selected first, then
the promo discount applies on top.

## Prerequisites — env (loyalty is OPTIONAL)

> **loyalty-cli is not a prerequisite for BSS-CLI.** It's an *adapter* you enable
> when you want promotions. With no loyalty configured, BSS-CLI runs completely
> normally — signup, orders, activation, renewal, the portal — the promo
> subsystem is simply off. See DECISIONS 2026-05-22 (graceful degradation).

To enable promotions, set (the catalog, COM, and CRM services each build a
loyalty client; the token never leaves those processes):

```bash
BSS_LOYALTY_BASE_URL=http://<loyalty-host>:8080   # loyalty-cli HTTP root
BSS_LOYALTY_API_TOKEN=<loyalty-cli's LOYALTY_API_TOKEN>
```

In the bundled compose these flow to every service via `env_file: .env`. Verify
loyalty is reachable: `curl -s $BSS_LOYALTY_BASE_URL/healthz` → `200`.

**When `BSS_LOYALTY_API_TOKEN` is unset:** catalog/COM/CRM still boot (a
`*.loyalty.disabled` warning is logged, no crash). `bss promo create` is rejected
(`catalog.promotion.loyalty_not_configured`); code validation returns "not
configured" so **orders proceed at full price**; the dashboard shows no offers;
customers are not mirrored into loyalty. Set the token and restart those three
services to turn promotions on.

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

## Targeted: an eligibility-gated code (v1.1.1)

A targeted promo is **one real loyalty code + a BSS eligibility list** — not a
codeless offer. The code exists in loyalty (visible in `promo_code.list`) but is
*not advertised*: it auto-applies only for customers on the eligibility list, and
a typed attempt by anyone else is rejected (`not_eligible`).

```bash
# 1. Create the promotion as targeted. A code is derived from the id if omitted
#    (it's BSS-internal); kind defaults to one-per-customer.
bss promo create --id PROMO_VIP --type percent --value 20 --duration single \
    --audience targeted

# 2. Add the chosen customers to its eligibility list.
bss promo assign --promo PROMO_VIP --customers CUST-001,CUST-007,CUST-042
```

`bss promo assign` is re-runnable: a customer already eligible is reported under
`already`. The code then auto-applies at each eligible customer's next order
(cheapest wins if they're eligible for several) and shows on their dashboard. For
repeatable demo data, `seed_targeted_campaign.py` at the repo root creates a
targeted promo and adds a set of customers to its eligibility list.

> **Why eligibility, not codeless?** loyalty's `promo_code.register` has no
> customer field, so the per-customer pairing lives in `catalog.promotion_eligibility`
> and BSS is the gate. See DECISIONS 2026-05-22 (v1.1.1).

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
- **Targeted promo didn't apply** — confirm the customer is on the eligibility
  list (re-run `bss promo assign`; it's idempotent), the promotion is `active`
  with `audience=targeted`, and the customer didn't untick it at signup.
- **`not_eligible` on a targeted code** — the customer typing it isn't on the
  eligibility list. Add them with `bss promo assign`, or it's working as intended
  (the code is private to its audience).

## Anti-patterns

- Don't expose `promo.create` / `promo.assign` to the customer chat surface — they
  are operator-only by doctrine. A customer types a code; they never issue one.
- Don't hand-edit `catalog.promotion` rows. The OfferDefinition link is owned by
  the create saga; a manual row with no loyalty OD is dead weight.
- Don't combine a typed code and a targeted offer expecting them to stack — order
  resolution applies one discount (typed code takes precedence; otherwise the best
  applicable assigned offer).
