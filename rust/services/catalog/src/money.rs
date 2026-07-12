//! Promo discount math — port of `bss_models.discount` (`apply_discount` +
//! `discount_label`).
//!
//! The effective price is always computed from the full base snapshot at charge
//! time (the base is never overwritten). This is the one place the create-time and
//! renewal-time math must agree, so it ports byte-for-byte: `rust_decimal` with
//! round-half-up to 2dp, and `normalize()` for the label (Python's
//! `format(value.normalize(), 'f')` drops trailing zeros: "20.00" → "20",
//! "12.50" → "12.5").

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

/// Human-readable discount label, e.g. `20% off` or `SGD 5.00 off`. `Err` on an
/// unknown discount type.
pub fn discount_label(
    discount_type: &str,
    discount_value: Decimal,
    currency: &str,
) -> Result<String, String> {
    match discount_type {
        // normalize() drops trailing zeros (20.00 → 20, 12.50 → 12.5), matching
        // Python's `format(value.normalize(), 'f')`.
        PERCENT => Ok(format!("{}% off", discount_value.normalize())),
        // {:.2} forces exactly 2dp (Python's `value.quantize(_CENTS)`): 5 → "5.00".
        ABSOLUTE => Ok(format!("{currency} {discount_value:.2} off")),
        other => Err(format!("unknown discount_type '{other}'")),
    }
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
    fn percent_discount_matches_worked_example() {
        // $25 base, 20% off → $20.00 (the plan's worked example).
        assert_eq!(
            apply_discount(PERCENT, d("20"), d("25.00")).unwrap(),
            d("20.00")
        );
        // 10% off $25 → $22.50 (the live DEMO_WELCOME10 case).
        assert_eq!(
            apply_discount(PERCENT, d("10.00"), d("25.00")).unwrap(),
            d("22.50")
        );
    }

    #[test]
    fn absolute_discount_floors_at_zero() {
        assert_eq!(
            apply_discount(ABSOLUTE, d("5"), d("25.00")).unwrap(),
            d("20.00")
        );
        // A discount never pays out.
        assert_eq!(
            apply_discount(ABSOLUTE, d("30"), d("25.00")).unwrap(),
            Decimal::ZERO
        );
    }

    #[test]
    fn rounds_half_up_to_two_places() {
        // 33% off 10.00 = 6.70; 15% off 9.99 = 8.4915 → 8.49.
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
        assert!(discount_label("bogus", d("5"), "SGD").is_err());
    }

    #[test]
    fn labels_drop_trailing_zeros() {
        assert_eq!(
            discount_label(PERCENT, d("20.00"), "SGD").unwrap(),
            "20% off"
        );
        assert_eq!(
            discount_label(PERCENT, d("12.50"), "SGD").unwrap(),
            "12.5% off"
        );
        assert_eq!(
            discount_label(PERCENT, d("10.00"), "SGD").unwrap(),
            "10% off"
        );
    }

    #[test]
    fn absolute_label_keeps_two_places() {
        assert_eq!(
            discount_label(ABSOLUTE, d("5.00"), "SGD").unwrap(),
            "SGD 5.00 off"
        );
        assert_eq!(
            discount_label(ABSOLUTE, d("5"), "SGD").unwrap(),
            "SGD 5.00 off"
        );
    }
}
