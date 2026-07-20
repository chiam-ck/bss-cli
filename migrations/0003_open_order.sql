-- Open Order + MSISDN Reservation — Phase 2 (persisted open order).
-- See docs/roadmap/open-order-msisdn-reservation.md.
--
-- The resumable, per-customer funnel record. Created at number-pick, keyed on the
-- verified email (owner_identity) — the customer record may not exist yet;
-- customer_id is linked at the create-customer step. The MSISDN soft hold
-- (0002) is taken with reserved_for = this row's id.
--
-- Lifecycle FSM: open -> completed | cancelled | expired. At most ONE open row
-- per owner (partial unique index) — starting a second funnel is blocked until
-- the current one completes or is cancelled.

CREATE TABLE IF NOT EXISTS inventory.open_order (
    id             text PRIMARY KEY,
    tenant_id      text NOT NULL DEFAULT 'DEFAULT',
    owner_identity text NOT NULL,
    customer_id    text,
    plan_code      text NOT NULL,
    msisdn         text,
    iccid          text,
    step           text NOT NULL DEFAULT 'pending_customer',
    status         text NOT NULL DEFAULT 'open',
    reserved_until timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- One open order per owner. Completed/cancelled/expired rows don't count, so a
-- customer can start a fresh funnel once the previous one is closed out.
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_order_owner_open
    ON inventory.open_order (owner_identity)
    WHERE status = 'open';

-- Sweep support (phase 4): find open orders past their hold window.
CREATE INDEX IF NOT EXISTS ix_open_order_reserved_until
    ON inventory.open_order (reserved_until)
    WHERE status = 'open';
