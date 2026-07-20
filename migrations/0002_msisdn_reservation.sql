-- Open Order + MSISDN Reservation — Phase 1 (reservation core).
-- See docs/roadmap/open-order-msisdn-reservation.md.
--
-- Adds a *soft hold* to the MSISDN pool: `reserved_until` set (+ `reserved_for`)
-- marks a temporary, releasable hold taken at number-pick. This is distinct from
-- the existing *hard reserve* (status='reserved' with reserved_until NULL) that
-- provisioning takes — so the two never collide and the 24h sweep (a later phase)
-- only ever releases rows whose `reserved_until` has passed.

ALTER TABLE inventory.msisdn_pool
    ADD COLUMN IF NOT EXISTS reserved_until timestamptz,
    ADD COLUMN IF NOT EXISTS reserved_for text;

-- Partial index for the sweep + the self-healing available/hold queries
-- (only soft holds carry a non-NULL reserved_until).
CREATE INDEX IF NOT EXISTS ix_msisdn_pool_reserved_until
    ON inventory.msisdn_pool (reserved_until)
    WHERE reserved_until IS NOT NULL;
