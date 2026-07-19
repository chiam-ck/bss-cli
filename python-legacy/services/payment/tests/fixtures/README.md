# Stripe sandbox fixtures (v0.16 Track 0)

Real Stripe test-mode responses + webhook deliveries, captured against the
sandbox account on 2026-05-03 and **redacted** so they're safe to commit. They
exist so Track 1 (`StripeTokenizerAdapter`) and Track 3 (webhook receiver)
can be built and tested without re-hitting Stripe's API on every test run.

## What's in here

| File | Source | Used by |
|---|---|---|
| `stripe_customer_sample.json` | `POST /v1/customers` response | `StripeTokenizerAdapter.ensure_customer` shape reference |
| `stripe_payment_intent_sample.json` | `POST /v1/payment_intents` response | `StripeTokenizerAdapter.charge` shape reference |
| `webhook_charge_succeeded.json` | `stripe trigger charge.succeeded` delivery | webhook receiver routing test |
| `webhook_charge_failed.json` | `stripe trigger charge.failed` delivery | webhook receiver routing test |
| `webhook_payment_intent_payment_failed.json` | `stripe trigger payment_intent.payment_failed` delivery | webhook receiver routing test |
| `webhook_charge_refunded.json` | `stripe trigger charge.refunded` delivery | webhook receiver routing test |
| `webhook_charge_dispute_created.json` | `stripe trigger charge.dispute.created` delivery | webhook receiver routing test |

## Redaction rules applied

All real identifiers are remapped to deterministic placeholders:

| Real | Placeholder pattern |
|---|---|
| `pi_3TSoct...` | `pi_FX_PI001`, `pi_FX_PI002`, … |
| `ch_3TSoct...` | `ch_FX_CH001`, … |
| `cus_URxxxx...` | `cus_FX_CUS001`, … |
| `pm_1TSoct...` | `pm_FX_PM001`, … |
| `evt_*`, `req_*`, `re_*`, `dp_*`, `txn_*` | analogous |

The placeholder suffix is always 12 chars, which is below the 14-char
threshold the doctrine guard greps for — so a fixture that accidentally
keeps a real id will trip the grep, and a redacted one passes.

Other neutralized fields:
- `receipt_url` → `https://pay.stripe.com/receipts/REDACTED`
- `client_secret` → fixed placeholder (the real one is single-use anyway)
- `network_transaction_id`, `fingerprint`, `ds_transaction_id` → constants
- `authorization_code` → `000000`

## Webhook signature verification

Each `webhook_*.json` was **re-signed** during redaction so Track 3 pytest
can verify deterministically without needing any real secret.

- **Test secret**: `whsec_test_fixture_v0160` (recorded in each fixture's
  `_fixture_meta.test_secret_value` field for clarity)
- **Test timestamp**: `1700000000` (2023-11-14 22:13:20 UTC) — well outside
  any real capture window so a stale-replay test can stamp `now=`
  `FIXTURE_TS + 1` deterministically
- **Algorithm**: identical to Stripe's wire format —
  `HMAC-SHA-256(secret, f"{timestamp}.{body}")`, hex
- **Verifier under test**: `bss_webhooks.signatures.verify_signature(scheme="stripe", …)`

The original real `Stripe-Signature` headers are discarded; the rewritten
header is what's in the fixture. The body is also rewritten as canonical
sorted JSON during redaction (Stripe's wire format is sorted, but their
indentation differs); both the rewritten body and rewritten signature line
up so verification is deterministic.

The original listen-session secret used during capture is dead the moment
`stripe listen` exits and is **not** persisted anywhere — neither in this
directory, in `.env`, nor in any conversation transcript that gets
committed.

## Doctrine guard (run before commit)

The v0.16 spec defines this guard at `phases/V0_16_0.md` line 61. The
forbidden patterns it greps for are: any Stripe live/test API key prefix
(secret or publishable), and any 14+-char id with the standard prefixes
(`pi_`, `pm_`, `cus_`, `cn_`, `evt_`). The literal regex isn't reproduced
here so this file itself doesn't trip the guard.

Empty output ⇒ pass. A non-empty hit means a real id leaked and the file
is unsafe to commit; re-run the redactor and check what was missed.

## Re-capturing

The capture + redact scripts live at `/tmp/v016_track0/` during Track 0
work and are not committed (they're a one-shot artifact, not durable
infrastructure). If a fixture needs a refresh:

1. `python3 capture.py 9999` (background)
2. `stripe listen --forward-to localhost:9999/webhooks/stripe`
3. `stripe trigger <event-name>`
4. `python3 redact.py` → moves redacted output into this directory
5. Run the doctrine grep above; commit if empty.
