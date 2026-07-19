//! Discount math — port of `bss_models.discount.apply_discount`.
//!
//! The effective price is always computed from the full base snapshot at charge
//! time (the base is never overwritten). This is the one place the create-time and
//! renewal-time math must agree with the catalog + order services, so it ports
//! byte-for-byte: `rust_decimal` round-half-up to 2dp (identical to catalog's
//! `money::apply_discount`).

use rust_decimal::{Decimal, RoundingStrategy};

pub const PERCENT: &str = "percent";
pub const ABSOLUTE: &str = "absolute";

/// Effective price after applying a discount to `base`:
/// - `percent`  → `base * (100 - value) / 100`
/// - `absolute` → `base - value` (floored at 0 — a discount never pays out)
///
/// Rounds half-up to 2dp. `Err` on an unknown discount type (callers validate
/// before persisting, so this is a backstop, mirroring the Python `ValueError`).
pub fn apply_discount(
    discount_type: &str,
    discount_value: Decimal,
    base_amount: Decimal,
) -> Result<Decimal, String> {
    let mut effective = match discount_type {
        PERCENT => base_amount * (Decimal::from(100) - discount_value) / Decimal::from(100),
        ABSOLUTE => base_amount - discount_value,
        other => return Err(format!("unknown discount_type '{other}'")),
    };
    if effective < Decimal::ZERO {
        effective = Decimal::ZERO;
    }
    Ok(effective.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn percent_matches_worked_example() {
        assert_eq!(
            apply_discount(PERCENT, d("20"), d("25.00")).unwrap(),
            d("20.00")
        );
        assert_eq!(
            apply_discount(PERCENT, d("10.00"), d("25.00")).unwrap(),
            d("22.50")
        );
    }

    #[test]
    fn absolute_floors_at_zero() {
        assert_eq!(
            apply_discount(ABSOLUTE, d("5"), d("25.00")).unwrap(),
            d("20.00")
        );
        assert_eq!(
            apply_discount(ABSOLUTE, d("30"), d("25.00")).unwrap(),
            Decimal::ZERO
        );
    }

    #[test]
    fn rounds_half_up() {
        assert_eq!(
            apply_discount(PERCENT, d("33"), d("10.00")).unwrap(),
            d("6.70")
        );
        assert_eq!(
            apply_discount(PERCENT, d("15"), d("9.99")).unwrap(),
            d("8.49")
        );
    }

    #[test]
    fn unknown_type_errors() {
        assert!(apply_discount("bogus", d("5"), d("25.00")).is_err());
    }
}
