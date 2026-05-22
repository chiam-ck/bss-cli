# DATA_MODEL.md — BSS-CLI (v3)

**Single PostgreSQL 16 instance. Schema-per-domain. ~38 tables across 12 schemas.**

Services NEVER read each other's schemas directly. The schema boundary is enforced socially in v0.1 and will be enforced mechanically (separate Postgres instances) in any later split. Each service configures its own `BSS_DB_URL` pointing at the shared instance.

All tables include:
- `tenant_id TEXT NOT NULL DEFAULT 'DEFAULT'` — future multi-tenancy
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` (where mutable)

IDs are TEXT with domain prefixes (`CUST-001`, `ORD-014`, `ICCID-...`, etc.).

## Schemas

```sql
CREATE SCHEMA crm;
CREATE SCHEMA catalog;
CREATE SCHEMA inventory;         -- MSISDN + eSIM pools
CREATE SCHEMA payment;
CREATE SCHEMA order_mgmt;        -- COM
CREATE SCHEMA service_inventory; -- SOM output (TMF638)
CREATE SCHEMA provisioning;      -- simulator
CREATE SCHEMA subscription;
CREATE SCHEMA mediation;
CREATE SCHEMA billing;
CREATE SCHEMA audit;
-- Post-v0.1: CREATE SCHEMA knowledge (pgvector for runbook RAG)
```

---

## Schema: `crm`

### `crm.party`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `PTY-xxx` |
| party_type | TEXT | `individual` \| `organization` (v0.1: individual only) |

### `crm.individual`
| column | type | notes |
|---|---|---|
| party_id | TEXT PK FK→party.id | |
| given_name | TEXT NOT NULL | |
| family_name | TEXT NOT NULL | |
| date_of_birth | DATE | |

### `crm.contact_medium`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `CM-xxx` |
| party_id | TEXT FK→party.id | |
| medium_type | TEXT | `email`, `phone`, `postal` |
| value | TEXT | |
| is_primary | BOOL | |
| valid_from | TIMESTAMPTZ | |
| valid_to | TIMESTAMPTZ | nullable |

Unique index `(medium_type, value) WHERE valid_to IS NULL AND medium_type='email'` — global email uniqueness for active records.

### `crm.customer`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `CUST-xxx` |
| party_id | TEXT FK→party.id | |
| status | TEXT | `pending`, `active`, `suspended`, `closed` |
| status_reason | TEXT | |
| customer_since | TIMESTAMPTZ | |
| kyc_status | TEXT NOT NULL DEFAULT 'not_verified' | `not_verified`, `pending`, `verified`, `rejected`, `expired` |
| kyc_verified_at | TIMESTAMPTZ | |
| kyc_verification_method | TEXT | `myinfo`, `jumio`, `onfido`, `manual`, `waived_test` |
| kyc_reference | TEXT | external verification ID from channel |
| kyc_expires_at | TIMESTAMPTZ | for jurisdictions requiring re-verification |

### `crm.customer_identity`

Minimum necessary identity data for regulatory compliance. **Document numbers are hashed, never stored plaintext.** Full plaintext lives in the attesting system (Myinfo, Jumio) or not at all.

| column | type | notes |
|---|---|---|
| customer_id | TEXT PK FK→customer.id | one identity per customer |
| document_type | TEXT NOT NULL | `nric`, `passport`, `fin`, `driving_license` |
| document_number_hash | TEXT NOT NULL | SHA-256 hex |
| document_country | CHAR(2) NOT NULL | ISO 3166-1 alpha-2 |
| date_of_birth | DATE NOT NULL | for age verification |
| nationality | CHAR(2) | |
| verified_by | TEXT | provider name |
| attestation_payload | JSONB | full signed attestation from channel |
| verified_at | TIMESTAMPTZ NOT NULL | |

Unique index on `(document_type, document_number_hash, tenant_id)` — one customer per document per tenant.

### `crm.interaction`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `INT-xxx` |
| customer_id | TEXT FK→customer.id | |
| channel | TEXT | `cli`, `llm`, `email`, `phone`, `scenario`, `system` |
| direction | TEXT | `inbound`, `outbound` |
| summary | TEXT NOT NULL | |
| body | TEXT | optional |
| agent_id | TEXT FK→agent.id | nullable |
| related_case_id | TEXT FK→case.id | nullable |
| related_ticket_id | TEXT FK→ticket.id | nullable |
| occurred_at | TIMESTAMPTZ NOT NULL | |

### `crm.agent`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `AGT-xxx` |
| name | TEXT NOT NULL | |
| email | TEXT UNIQUE | |
| role | TEXT | `csr`, `supervisor`, `engineer`, `system` |
| status | TEXT | `active`, `terminated` |

Seed: 5 agents (Alice, Bob, Carol, Dave, System).

### `crm.case`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `CASE-xxx` |
| customer_id | TEXT FK→customer.id | |
| subject | TEXT NOT NULL | |
| description | TEXT | |
| state | TEXT | `open`, `in_progress`, `pending_customer`, `resolved`, `closed` |
| priority | TEXT | `low`, `normal`, `high`, `urgent` |
| category | TEXT | `billing`, `technical`, `general`, `complaint` |
| resolution_code | TEXT | required to close |
| opened_by_agent_id | TEXT FK→agent.id | |
| opened_at | TIMESTAMPTZ NOT NULL | |
| closed_at | TIMESTAMPTZ | |

### `crm.case_note`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| case_id | TEXT FK→case.id | |
| author_agent_id | TEXT FK→agent.id | |
| body | TEXT NOT NULL | |
| created_at | TIMESTAMPTZ | |

### `crm.ticket`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `TKT-xxx` |
| case_id | TEXT FK→case.id | nullable |
| customer_id | TEXT FK→customer.id | denormalized |
| ticket_type | TEXT | `service_outage`, `billing_dispute`, `configuration`, `information_request`, `provisioning_exception` |
| subject | TEXT NOT NULL | |
| description | TEXT | |
| state | TEXT | `open`, `acknowledged`, `in_progress`, `pending`, `resolved`, `closed`, `cancelled` |
| priority | TEXT | |
| assigned_to_agent_id | TEXT FK→agent.id | |
| related_order_id | TEXT | nullable |
| related_subscription_id | TEXT | nullable |
| related_service_id | TEXT | nullable |
| sla_due_at | TIMESTAMPTZ | |
| resolution_notes | TEXT | required to resolve |
| opened_at | TIMESTAMPTZ NOT NULL | |
| resolved_at | TIMESTAMPTZ | |
| closed_at | TIMESTAMPTZ | |

### `crm.ticket_state_history`
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| ticket_id | TEXT FK→ticket.id | |
| from_state | TEXT | |
| to_state | TEXT | |
| changed_by_agent_id | TEXT FK→agent.id | |
| reason | TEXT | |
| event_time | TIMESTAMPTZ | |

### `crm.sla_policy`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| ticket_type | TEXT | |
| priority | TEXT | |
| target_resolution_minutes | INTEGER | |

Seed: 12 rows (3 ticket types × 4 priorities for core types).

### `crm.port_request` (v0.17)

Operator-driven MNP request. Distinct aggregate from `crm.case` by doctrine
(CLAUDE.md v0.17+ anti-pattern) — port requests have a fixed FSM and a
specific data shape that doesn't fit Case's customer-incident semantics.

| column | type | notes |
|---|---|---|
| id | TEXT PK | `PORT-NNNN` (uuid4 hex slice) |
| direction | TEXT | CHECK in (`port_in`, `port_out`) |
| donor_carrier | TEXT NOT NULL | free-text carrier name |
| donor_msisdn | TEXT NOT NULL | the number being ported |
| target_subscription_id | TEXT | NULL OK for port-in pre-activation; required for port-out |
| requested_port_date | DATE NOT NULL | |
| state | TEXT | CHECK in (`requested`, `validated`, `completed`, `rejected`); default `requested` |
| rejection_reason | TEXT | populated on reject |
| created_at, updated_at | TIMESTAMPTZ | |
| tenant_id | TEXT | default `'DEFAULT'` |

Indexes:
- `uq_port_request_donor_pending` — partial unique on `(donor_msisdn, tenant_id) WHERE state IN ('requested','validated')`. Mirrors the policy `port_request.create.donor_msisdn_unique_among_pending` so two live ports of the same donor MSISDN can never coexist; completed/rejected rows can stack without blocking a future re-port.
- `ix_port_request_state_direction` — backs the `/ports` list page.

FSM: `services/crm/app/domain/port_request_state.py`. v0.17 ships only the operator-driven `requested → completed | rejected` path; `validated` is a hook for a future automated donor-carrier check.

---

## Schema: `inventory`

### `inventory.msisdn_pool`
| column | type | notes |
|---|---|---|
| msisdn | TEXT PK | `90000000` |
| status | TEXT | `available`, `reserved`, `assigned`, `quarantine`, `ported_out` (v0.17, terminal) |
| reserved_at | TIMESTAMPTZ | |
| assigned_to_subscription_id | TEXT | nullable |
| quarantine_until | TIMESTAMPTZ | 90 days after termination; v0.17 also `'9999-12-31'` for `ported_out` |

Seed: 1000 numbers `90000000`-`90000999`, all `available`. Operator-driven replenishment (v0.17): `bss inventory msisdn add-range <prefix> <count>` bulk-inserts via `INSERT ... ON CONFLICT DO NOTHING`. Doctrine v0.17+: `ported_out` is terminal — port-out approve flips the row and reserve-next never selects it again (predicate is `status='available'`).

### `inventory.esim_profile`

**eSIM-only inventory.** No physical SIM in v0.1.

| column | type | notes |
|---|---|---|
| iccid | TEXT PK | `8910...` 19-20 digit |
| imsi | TEXT UNIQUE NOT NULL | real identifier on network |
| ki_ref | TEXT NOT NULL | **reference to HSM, NEVER the actual key** |
| profile_state | TEXT | `available`, `reserved`, `downloaded`, `activated`, `suspended`, `recycled` |
| smdp_server | TEXT | `smdp.bss-cli.local` (simulated) |
| matching_id | TEXT UNIQUE | one-time code for eSIM download |
| activation_code | TEXT | full LPA string: `LPA:1$smdp.bss-cli.local$MATCHING_ID` |
| assigned_msisdn | TEXT FK→msisdn_pool.msisdn | nullable |
| assigned_to_subscription_id | TEXT | nullable |
| reserved_at | TIMESTAMPTZ | |
| downloaded_at | TIMESTAMPTZ | |
| activated_at | TIMESTAMPTZ | |

Seed: 1000 profiles with generated ICCIDs, IMSIs, stubbed ki_refs (`hsm://ref/<uuid>`), all `available`.

**Critical:** `ki_ref` is a string reference to a hypothetical HSM slot. The actual Ki value is NEVER in this database — documenting the boundary is more important than simulating the contents. Real Ki storage is HSM territory and is explicitly out of scope.

---

## Schema: `catalog`

### `catalog.product_specification`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| name | TEXT | |
| description | TEXT | |
| brand | TEXT | |
| lifecycle_status | TEXT | |

Seed: `SPEC_MOBILE_PREPAID`.

### `catalog.product_offering`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `PLAN_S`, `PLAN_M`, `PLAN_L` |
| name | TEXT | |
| spec_id | TEXT FK | |
| is_bundle | BOOL DEFAULT TRUE | |
| is_sellable | BOOL | |
| lifecycle_status | TEXT | |
| valid_from | TIMESTAMPTZ | |
| valid_to | TIMESTAMPTZ | |

### `catalog.product_offering_price`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| offering_id | TEXT FK | |
| price_type | TEXT | `recurring`, `one_time` |
| recurring_period_length | INTEGER | |
| recurring_period_type | TEXT | `month` |
| amount | NUMERIC(12,2) | |
| currency | CHAR(3) | `SGD` |
| valid_from | TIMESTAMPTZ | v0.7+ — NULL = always-active |
| valid_to | TIMESTAMPTZ | v0.7+ — exclusive boundary; NULL = no end |

### Catalog versioning (v0.7+)

Both `product_offering` and `product_offering_price` are **time-bounded**.
A NULL `valid_from` means "active since the dawn of time"; a NULL
`valid_to` means "active until further notice". Active queries
(`list_active_offerings`, `get_active_price`) filter rows where
`valid_from IS NULL OR valid_from <= now` AND `valid_to IS NULL OR
valid_to > now`. The `valid_to` boundary is **exclusive** so two rows
can be set up "back-to-back" without overlap.

When multiple price rows are simultaneously active for the same
offering (e.g. base $25 + windowed promo $20), **lowest amount wins**.
This is the only discount semantic v0.7 supports — coupons, stacked
discounts, and A/B price tests are out of scope.

The renewal stack **never** queries active prices. Each subscription
carries a price snapshot (see `subscription.subscription` columns
`price_amount`, `price_currency`, `price_offering_price_id`) captured
at order-creation time. Catalog repricings do not silently affect
existing customers; explicit operator-initiated migrations
(`subscription.migrate_to_new_price`) carry a notice period and
emit per-subscription `notification.requested` events.

### `catalog.promotion` (v1.1)

The money terms for a promotion + the join key to a loyalty-cli OfferDefinition.
The only new domain object in v1.1; loyalty owns the entitlement (codes/offers,
limits, inventory, targeting), this row owns the discount. No FK to loyalty (HTTP
boundary). The discount **composes** on top of the lowest-active price snapshot.

| column | type | notes |
|---|---|---|
| id | TEXT PK | e.g. `PROMO_SUMMER25` |
| name | TEXT | v1.1.1 — operator-set friendly label for customer display ("VIP Welcome"); NULL → UI falls back to the discount label |
| code | TEXT | the loyalty code (BOTH audiences, v1.1.1). Public = advertised/typed; targeted = unadvertised, derived from id if omitted. Partial-unique on `(code, tenant_id)` |
| audience | TEXT | `public` (anyone may type) \| `targeted` (eligibility-gated, auto-applied). v1.1.1 (CHECK) |
| offer_definition_id | TEXT | loyalty join key; NULL until the create saga completes |
| discount_type | TEXT | `percent` \| `absolute` (CHECK) |
| discount_value | NUMERIC(12,2) | percent 0-100, or absolute amount |
| currency | TEXT | default `SGD` (for absolute) |
| applicable_offering_ids | TEXT[] | NULL = all sellable offerings |
| duration_kind | TEXT | `single` \| `multi` \| `perpetual` (CHECK); multi ⇔ periods_total set (CHECK) |
| periods_total | SMALLINT | N for multi; NULL for single/perpetual |
| valid_from / valid_to | TIMESTAMPTZ | optional validity window |
| state | TEXT | `pending_link` → `active` → `retired` (CHECK) |
| created_by | TEXT | operator actor |

Index `ix_promotion_offer_definition_id` backs the OD→terms lookup (validate,
resolve, reconcile). A promotion stays `pending_link` until loyalty's
`offer_definition.register` + `promo_code.register` returns; a live code does
nothing until the row is `active`, so a half-failed create saga is harmless and
resumable.

### `catalog.promotion_eligibility` (v1.1.1)

Which customers may use a **targeted** promotion's code (loyalty's `promo_code`
has no customer field, so the per-customer pairing lives here; BSS is the gate).
A targeted code auto-applies for customers with a row here and is rejected
(`not_eligible`) for anyone without one. Public promos have no rows.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| promotion_id | TEXT FK→catalog.promotion.id | |
| customer_id | TEXT | |
| created_by | TEXT | operator actor |

Unique on `(promotion_id, customer_id, tenant_id)` (idempotent assign); index on
`(customer_id, tenant_id)` for the order-time "what's this customer eligible for?"
lookup.

### `catalog.bundle_allowance`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| offering_id | TEXT FK | |
| allowance_type | TEXT | `data`, `voice`, `sms`, `data_roaming` (v0.17) |
| quantity | BIGINT | -1 = unlimited |
| unit | TEXT | `mb`, `minutes`, `count` |

v0.17 — `data_roaming` is an **additive** allowance type. PLAN_S has 0 mb, PLAN_M has 500 mb, PLAN_L has 2048 mb. Unlike `data`, exhaustion of `data_roaming` does NOT block the subscription — the policy `subscription.usage_rated.roaming_balance_required` rejects roaming usage independently while home `data` keeps flowing.

### `catalog.vas_offering`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| name | TEXT | |
| price_amount | NUMERIC(12,2) | |
| currency | CHAR(3) | |
| allowance_type | TEXT | |
| allowance_quantity | BIGINT | |
| allowance_unit | TEXT | |
| expiry_hours | INTEGER | nullable |

### `catalog.service_specification`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| name | TEXT | |
| type | TEXT | `CFS` or `RFS` |
| parameters | JSONB | |

Seed: `SSPEC_CFS_MOBILE_BROADBAND`, `SSPEC_RFS_DATA_BEARER`, `SSPEC_RFS_VOICE_BEARER`.

### `catalog.product_to_service_mapping`
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| offering_id | TEXT FK | |
| cfs_spec_id | TEXT FK | |
| rfs_spec_ids | TEXT[] | array |

Seed: all three plans → same CFS + two RFS.

---

## Schema: `payment`

### `payment.payment_method`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `PM-xxx` |
| customer_id | TEXT | cross-service ref, no FK |
| type | TEXT | `card` |
| token | TEXT UNIQUE | `tok_<uuid>` |
| last4 | CHAR(4) | |
| brand | TEXT | |
| exp_month | SMALLINT | |
| exp_year | SMALLINT | |
| is_default | BOOL | |
| status | TEXT | `active`, `removed` |

### `payment.payment_attempt`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `PAY-xxx` |
| customer_id | TEXT | |
| payment_method_id | TEXT FK→payment_method.id | |
| amount | NUMERIC(12,2) | |
| currency | CHAR(3) | |
| purpose | TEXT | `activation`, `renewal`, `vas` |
| status | TEXT | `pending`, `approved`, `declined`, `error` |
| gateway_ref | TEXT | `mock_<uuid>` |
| decline_reason | TEXT | |
| attempted_at | TIMESTAMPTZ | |

---

## Schema: `order_mgmt` (COM)

### `order_mgmt.product_order`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `ORD-xxx` |
| customer_id | TEXT | |
| state | TEXT | `acknowledged`, `in_progress`, `completed`, `failed`, `cancelled` |
| order_date | TIMESTAMPTZ | |
| requested_completion_date | TIMESTAMPTZ | |
| completed_date | TIMESTAMPTZ | |
| msisdn_preference | TEXT | nullable — "golden number" request |
| notes | TEXT | |

### `order_mgmt.order_item`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| order_id | TEXT FK | |
| action | TEXT | `add`, `modify`, `delete` |
| offering_id | TEXT | |
| state | TEXT | |
| target_subscription_id | TEXT | nullable |
| price_amount | NUMERIC(10,2) | v0.7+ — snapshot stamped at create_order |
| price_currency | TEXT | v0.7+ |
| price_offering_price_id | TEXT | v0.7+ — copied to subscription on activation |
| discount_code | TEXT | v1.1 — typed promo code (NULL for targeted/no-promo) |
| promo_offer_definition_id | TEXT | v1.1 — loyalty OD; discount INTENT stamped at create |
| discount_type | TEXT | v1.1 — `percent`\|`absolute` |
| discount_value | NUMERIC(12,2) | v1.1 |
| discount_periods_total | SMALLINT | v1.1 — single=1, multi=N, perpetual=-1 |
| promo_offer_id | TEXT | v1.1 — loyalty offer id; set at create for assigned offers, at claim for typed codes |

### `order_mgmt.order_state_history`
Standard state history shape.

---

## Schema: `service_inventory` (SOM output, TMF638)

### `service_inventory.service_order`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `SO-xxx` |
| commercial_order_id | TEXT | |
| state | TEXT | `acknowledged`, `in_progress`, `completed`, `failed`, `cancelled` |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | |

### `service_inventory.service_order_item`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| service_order_id | TEXT FK | |
| action | TEXT | |
| service_spec_id | TEXT | |
| target_service_id | TEXT | |

### `service_inventory.service`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `SVC-xxx` |
| subscription_id | TEXT | |
| spec_id | TEXT | |
| type | TEXT | `CFS` or `RFS` |
| parent_service_id | TEXT FK→service.id | nullable |
| state | TEXT | `feasibility_checked`, `designed`, `reserved`, `activated`, `terminated` |
| characteristics | JSONB | e.g. `{"msisdn": "90000005", "iccid": "8910...", "apn": "internet"}` |
| activated_at | TIMESTAMPTZ | |
| terminated_at | TIMESTAMPTZ | |

### `service_inventory.service_state_history`
Standard state history shape.

---

## Schema: `provisioning` (simulator)

### `provisioning.provisioning_task`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `PTK-xxx` |
| service_id | TEXT | |
| task_type | TEXT | `HLR_PROVISION`, `PCRF_POLICY_PUSH`, `OCS_BALANCE_INIT`, `ESIM_PROFILE_PREPARE`, `HLR_DEPROVISION` |
| state | TEXT | `pending`, `running`, `completed`, `failed`, `stuck` |
| attempts | SMALLINT DEFAULT 0 | |
| max_attempts | SMALLINT DEFAULT 3 | |
| payload | JSONB | |
| last_error | TEXT | |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | |

### `provisioning.fault_injection`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| task_type | TEXT | |
| fault_type | TEXT | `fail_first_attempt`, `fail_always`, `stuck`, `slow` |
| probability | NUMERIC(3,2) | |
| enabled | BOOL | |

Seed: 6 rules covering HLR_PROVISION, PCRF_POLICY_PUSH, OCS_BALANCE_INIT, ESIM_PROFILE_PREPARE, HLR_DEPROVISION — all disabled by default.

---

## Schema: `subscription`

### `subscription.subscription`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `SUB-xxx` |
| customer_id | TEXT | |
| offering_id | TEXT | |
| msisdn | TEXT UNIQUE | |
| iccid | TEXT UNIQUE | the eSIM binding |
| cfs_service_id | TEXT | |
| state | TEXT | `pending`, `active`, `blocked`, `terminated` |
| state_reason | TEXT | |
| activated_at | TIMESTAMPTZ | |
| current_period_start | TIMESTAMPTZ | |
| current_period_end | TIMESTAMPTZ | |
| next_renewal_at | TIMESTAMPTZ | |
| terminated_at | TIMESTAMPTZ | |
| last_renewal_attempted_at | TIMESTAMPTZ | v0.18+ — written by the renewal worker BEFORE dispatching `renew()` so a peer replica or a re-fired tick doesn't double-charge the same period boundary. Compared via `last_renewal_attempted_at < next_renewal_at` — when `next_renewal_at` advances after a successful renewal the row naturally becomes "due" again next period without any cleanup. Reused by the blocked-overdue sweep to dedup `subscription.renewal_skipped` events. |
| price_amount | NUMERIC(10,2) NOT NULL | v0.7+ snapshot — drives renewal charge |
| price_currency | TEXT NOT NULL | v0.7+ snapshot |
| price_offering_price_id | TEXT NOT NULL FK→catalog.product_offering_price | v0.7+ snapshot |
| pending_offering_id | TEXT | v0.7+ — set when a plan change / price migration is scheduled |
| pending_offering_price_id | TEXT | v0.7+ — same |
| pending_effective_at | TIMESTAMPTZ | v0.7+ — earliest moment renewal applies the pending pivot |
| discount_type | TEXT | v1.1 — `percent`\|`absolute`; NULL = no promo |
| discount_value | NUMERIC(12,2) | v1.1 |
| discount_periods_remaining | SMALLINT NOT NULL DEFAULT 0 | v1.1 — discounted periods left; `renew()` decrements while >0; `-1` = perpetual |
| promo_code | TEXT | v1.1 — forensic |
| promo_offer_definition_id | TEXT | v1.1 — forensic / loyalty join |

v1.1 — `price_amount` stays the **full base** snapshot; the effective per-period
charge is `apply_discount(discount_type, discount_value, price_amount)` while
`discount_periods_remaining` is live (>0, or -1 perpetual). A pending plan change
clears all discount fields at the renewal pivot (a plan change ends the promo).

Index `ix_subscription_due_for_renewal` on `(state, next_renewal_at) WHERE state IN ('active','blocked')` (v0.18) backs both renewal-worker sweep queries.

### `subscription.bundle_balance`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| subscription_id | TEXT FK→subscription.id | |
| allowance_type | TEXT | `data`, `voice`, `sms`, `data_roaming` (v0.17) |
| total | BIGINT | |
| consumed | BIGINT | |
| remaining | BIGINT GENERATED ALWAYS AS (total - consumed) STORED | |
| unit | TEXT | |
| period_start | TIMESTAMPTZ | |
| period_end | TIMESTAMPTZ | |

Typed-row model — one row per (subscription, allowance_type). New subscriptions on PLAN_M / PLAN_L get a `data_roaming` row materialized from `catalog.bundle_allowance`; PLAN_S gets a row with `total=0` (so the snapshot doctrine still holds at create-time) but the portal line_card filter hides the bar when `total=0 AND remaining=0`. v0.17 — `purchase_vas` materializes a missing balance row from the VAS spec rather than silently no-op'ing, so VAS_ROAMING_1GB top-up against a roaming-less subscription works without pre-seeding.

### `subscription.vas_purchase`
| column | type | notes |
|---|---|---|
| id | TEXT PK | |
| subscription_id | TEXT FK | |
| vas_offering_id | TEXT | |
| payment_attempt_id | TEXT | |
| applied_at | TIMESTAMPTZ | |
| expires_at | TIMESTAMPTZ | |
| allowance_added | BIGINT | |
| allowance_type | TEXT | |

### `subscription.subscription_state_history`
Standard state history shape.

---

## Schema: `mediation`

### `mediation.usage_event`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `UE-xxx` |
| msisdn | TEXT | |
| subscription_id | TEXT | enriched |
| event_type | TEXT | `data`, `voice`, `voice_minutes`, `sms` |
| event_time | TIMESTAMPTZ | |
| quantity | BIGINT | |
| unit | TEXT | |
| source | TEXT | `simulator`, `cli`, `scenario` |
| raw_cdr_ref | TEXT | |
| processed | BOOL DEFAULT FALSE | |
| processing_error | TEXT | |
| roaming_indicator | BOOL NOT NULL DEFAULT FALSE | v0.17 — set by the channel/network adapter when the underlying CDR was produced on a visited network. Server-default false preserves backwards compat with pre-v0.17 callers. The rating consumer reads this from the `usage.recorded` payload and, when true with `event_type=data`, routes the decrement to the `data_roaming` BundleBalance instead of `data`. Doctrine v0.17+: roaming is a per-event *attribute*, not an event-type discriminator. |

---

## Schema: `billing` (scaffolded — unused in v0.1)

The `billing` schema and its tables (`billing_account`, `customer_bill`) are scaffolded in the Phase 2 initial migration but no service code reads or writes them in v0.1. v0.1.1 formally defers the billing service to v0.2, where it will be reintroduced as a read-only view layer over `payment.payment_attempt` (receipt aggregation, TMF678 `/customerBill` endpoints, statement generation — no dunning, no credit extension, no formal invoice generation). The table definitions below describe the Phase 2 migration shape and are left in place so v0.2 is purely additive. See `DECISIONS.md` 2026-04-13 for the deferral rationale.

### `billing.billing_account`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `BA-xxx` |
| customer_id | TEXT UNIQUE | one per customer |
| payment_method_id | TEXT | default COF |
| currency | CHAR(3) | |

### `billing.customer_bill`
| column | type | notes |
|---|---|---|
| id | TEXT PK | `BILL-xxx` |
| billing_account_id | TEXT FK | |
| subscription_id | TEXT | |
| period_start | TIMESTAMPTZ | |
| period_end | TIMESTAMPTZ | |
| amount | NUMERIC(12,2) | |
| currency | CHAR(3) | |
| status | TEXT | `issued`, `paid`, `failed` |
| payment_attempt_id | TEXT | |
| issued_at | TIMESTAMPTZ | |
| paid_at | TIMESTAMPTZ | |

---

## Schema: `audit`

### `audit.domain_event`

The outbox and replay substrate. Every meaningful state change writes here in the same transaction as the domain write.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| event_id | UUID UNIQUE | |
| event_type | TEXT | e.g., `subscription.exhausted` |
| aggregate_type | TEXT | `subscription`, `order`, `ticket`, etc. |
| aggregate_id | TEXT | |
| occurred_at | TIMESTAMPTZ | |
| trace_id | TEXT | forward-compat for OTel (Phase 11) |
| actor | TEXT | from `auth_context.current().actor` |
| channel | TEXT | from `X-BSS-Channel` header |
| tenant_id | TEXT | |
| payload | JSONB | |
| schema_version | SMALLINT | |
| published_to_mq | BOOL DEFAULT FALSE | flip to TRUE after RabbitMQ publish |

Indexes:
- `(aggregate_type, aggregate_id, occurred_at)` for replay
- `(event_type, occurred_at)` for reporting
- `(published_to_mq) WHERE NOT published_to_mq` partial index for the replay job (post-v0.1)

---

## Schema: `knowledge` (post-v0.1)

Documented here for forward-compat. Not created in v0.1 migration.

### `knowledge.runbook_document`
| column | type | notes |
|---|---|---|
| id | TEXT PK | slug, e.g. `troubleshooting/data_not_working` |
| title | TEXT | |
| category | TEXT | |
| source_path | TEXT | path in repo |
| content_sha256 | TEXT | cache key for re-indexing |
| updated_at | TIMESTAMPTZ | |

### `knowledge.runbook_chunk`
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| document_id | TEXT FK | |
| chunk_idx | INTEGER | |
| content | TEXT | |
| embedding | vector(384) | pgvector, BGE-small dim |
| tokens | INTEGER | |

Enabled in Phase 11 via `CREATE EXTENSION vector`. Indexed via `ivfflat (embedding vector_cosine_ops)`.

---

## Table count

| Schema | Tables |
|---|---|
| crm | 10 (adds `customer_identity`) |
| catalog | 7 |
| inventory | 2 (adds `esim_profile`) |
| payment | 2 |
| order_mgmt | 3 |
| service_inventory | 4 |
| provisioning | 2 |
| subscription | 4 |
| mediation | 1 |
| billing | 2 |
| audit | 1 |

**Total for v0.1: 38 tables across 11 schemas.** Up from 36 in v2 (added `customer_identity` and `esim_profile`).

`knowledge` schema (2 more tables) added in Phase 11.

## Seed data summary

- 3 product offerings (S, M, L) with prices and allowances
- 3 VAS offerings
- 1 product spec, 3 service specs, 3 product-to-service mappings
- 1000 MSISDN numbers (`90000000`-`90000999`)
- 1000 eSIM profiles with stubbed ki_refs
- 5 agents
- 12 SLA policies
- 6 fault injection rules (all disabled)

Seed customers deferred to Phase 10 scenarios.
