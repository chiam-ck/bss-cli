//! Pure domain logic — port of `app.domain.bundle` + `app.domain.state_machine`.
//!
//! No DB, no clock, no side effects. `BalanceSnapshot` mirrors the frozen
//! dataclass; the state machine is the same 4-state FSM (pending/active/blocked/
//! terminated) with the same triggers. All the block-on-exhaust correctness lives
//! here and is unit-tested against the Python behaviour.

pub const UNLIMITED: i64 = -1;

/// Immutable view of a single allowance balance.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BalanceSnapshot {
    pub allowance_type: String,
    pub total: i64, // -1 = unlimited
    pub consumed: i64,
    pub unit: String,
}

impl BalanceSnapshot {
    pub fn remaining(&self) -> i64 {
        if self.total == UNLIMITED {
            UNLIMITED
        } else {
            self.total - self.consumed
        }
    }
}

/// Plan-level allowance definition (from catalog).
#[derive(Debug, Clone)]
pub struct AllowanceSpec {
    pub allowance_type: String,
    pub quantity: i64, // -1 = unlimited
    pub unit: String,
}

/// Decrement `consumed` by `quantity`, clamped so remaining never goes negative.
/// Unlimited balances (-1 total) are never decremented. Negative quantity panics
/// in Python (`ValueError`); here we clamp at 0 defensively — callers pass the
/// non-negative `consumedQuantity` off the event.
pub fn consume(balance: &BalanceSnapshot, quantity: i64) -> BalanceSnapshot {
    if quantity <= 0 || balance.total == UNLIMITED {
        return balance.clone();
    }
    let new_consumed = (balance.consumed + quantity).min(balance.total);
    BalanceSnapshot {
        allowance_type: balance.allowance_type.clone(),
        total: balance.total,
        consumed: new_consumed,
        unit: balance.unit.clone(),
    }
}

/// True if the primary allowance type has remaining <= 0. Unlimited never
/// exhausts. If no balance matches `primary_type`, returns true (no data =
/// exhausted). v0.17: `data_roaming` is additive, never primary — callers keep
/// `primary_type = "data"`.
pub fn is_exhausted(balances: &[BalanceSnapshot], primary_type: &str) -> bool {
    for b in balances {
        if b.allowance_type == primary_type {
            if b.total == UNLIMITED {
                return false;
            }
            return b.remaining() <= 0;
        }
    }
    true
}

/// Top-up: increase total by quantity. Unlimited balances are unchanged.
pub fn add_allowance(balance: &BalanceSnapshot, quantity: i64) -> BalanceSnapshot {
    if quantity <= 0 || balance.total == UNLIMITED {
        return balance.clone();
    }
    BalanceSnapshot {
        allowance_type: balance.allowance_type.clone(),
        total: balance.total + quantity,
        consumed: balance.consumed,
        unit: balance.unit.clone(),
    }
}

/// Renewal: fresh balances from plan specs with consumed=0.
pub fn reset_for_new_period(specs: &[AllowanceSpec]) -> Vec<BalanceSnapshot> {
    specs
        .iter()
        .map(|s| BalanceSnapshot {
            allowance_type: s.allowance_type.clone(),
            total: s.quantity,
            consumed: 0,
            unit: s.unit.clone(),
        })
        .collect()
}

pub const PRIMARY_ALLOWANCE_TYPE: &str = "data";

// ── state machine ───────────────────────────────────────────────────────────

/// (trigger, source, dest). Mirrors `app.domain.state_machine.TRANSITIONS`.
const TRANSITIONS: &[(&str, &str, &str)] = &[
    ("activate", "pending", "active"),
    ("fail_activate", "pending", "terminated"),
    ("exhaust", "active", "blocked"),
    ("top_up", "blocked", "active"),
    ("top_up", "active", "active"),
    ("renew", "active", "active"),
    ("renew_fail", "active", "blocked"),
    ("terminate", "active", "terminated"),
    ("terminate", "blocked", "terminated"),
];

pub fn is_valid_transition(from_state: &str, trigger: &str) -> bool {
    TRANSITIONS
        .iter()
        .any(|(t, src, _)| *t == trigger && *src == from_state)
}

pub fn get_next_state(from_state: &str, trigger: &str) -> Option<&'static str> {
    TRANSITIONS
        .iter()
        .find(|(t, src, _)| *t == trigger && *src == from_state)
        .map(|(_, _, dest)| *dest)
}

/// Discounted-periods counter AFTER the activation charge (period 1). No discount
/// → 0. Perpetual (total = -1) → -1. Otherwise `total - 1`, floored at 0. Port of
/// `_initial_discount_remaining`.
pub fn initial_discount_remaining(
    discount_type: Option<&str>,
    discount_periods_total: Option<i64>,
) -> i64 {
    match (discount_type, discount_periods_total) {
        (Some(_), Some(total)) if total < 0 => -1,
        (Some(_), Some(total)) => (total - 1).max(0),
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bal(t: &str, total: i64, consumed: i64) -> BalanceSnapshot {
        BalanceSnapshot {
            allowance_type: t.to_string(),
            total,
            consumed,
            unit: "mb".to_string(),
        }
    }

    #[test]
    fn consume_clamps_at_total() {
        let b = bal("data", 100, 90);
        assert_eq!(consume(&b, 50).consumed, 100);
        assert_eq!(consume(&b, 5).consumed, 95);
        assert_eq!(consume(&b, 0).consumed, 90);
    }

    #[test]
    fn consume_never_touches_unlimited() {
        let b = bal("data", UNLIMITED, 500);
        assert_eq!(consume(&b, 999).consumed, 500);
    }

    #[test]
    fn is_exhausted_primary_only() {
        assert!(is_exhausted(&[bal("data", 100, 100)], "data"));
        assert!(!is_exhausted(&[bal("data", 100, 50)], "data"));
        // unlimited data never exhausts
        assert!(!is_exhausted(&[bal("data", UNLIMITED, 9999)], "data"));
        // roaming exhausted but data fine → not exhausted (roaming is additive)
        assert!(!is_exhausted(
            &[bal("data", 100, 10), bal("data_roaming", 50, 50)],
            "data"
        ));
        // no data row at all → exhausted
        assert!(is_exhausted(&[bal("voice", 100, 0)], "data"));
    }

    #[test]
    fn add_allowance_tops_up() {
        assert_eq!(add_allowance(&bal("data", 100, 20), 50).total, 150);
        assert_eq!(
            add_allowance(&bal("data", UNLIMITED, 20), 50).total,
            UNLIMITED
        );
    }

    #[test]
    fn state_machine_transitions() {
        assert!(is_valid_transition("pending", "activate"));
        assert!(!is_valid_transition("pending", "renew"));
        assert!(is_valid_transition("active", "exhaust"));
        assert!(is_valid_transition("blocked", "top_up"));
        assert!(!is_valid_transition("terminated", "renew"));
        assert_eq!(get_next_state("active", "exhaust"), Some("blocked"));
        assert_eq!(get_next_state("blocked", "top_up"), Some("active"));
        assert_eq!(get_next_state("active", "renew_fail"), Some("blocked"));
        assert_eq!(get_next_state("terminated", "terminate"), None);
    }

    #[test]
    fn discount_remaining_after_activation() {
        assert_eq!(initial_discount_remaining(None, None), 0);
        assert_eq!(initial_discount_remaining(Some("percent"), Some(-1)), -1);
        assert_eq!(initial_discount_remaining(Some("percent"), Some(1)), 0);
        assert_eq!(initial_discount_remaining(Some("percent"), Some(3)), 2);
    }
}
