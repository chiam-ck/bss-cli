//! Idempotency-key construction for outbound provider calls. Port of
//! `bss_webhooks.idempotency`.
//!
//! Same key on a BSS-crash-restart retry → provider dedupes (reuse the recorded
//! key); new key on a user-initiated retry → fresh attempt. Encoded by the
//! `(aggregate_id, retry_count)` pair. Single source of key format so callers
//! can't drift it.

/// Build a deterministic idempotency key: `"<AGGREGATE_ID>-r<retry_count>"`
/// (e.g. `"ATT-0042-r0"`). Errors on an empty id or a negative retry count.
pub fn idempotency_key(aggregate_id: &str, retry_count: i64) -> Result<String, String> {
    if aggregate_id.is_empty() {
        return Err("aggregate_id is required for idempotency_key".to_string());
    }
    if retry_count < 0 {
        return Err(format!("retry_count must be >= 0, got {retry_count}"));
    }
    Ok(format!("{aggregate_id}-r{retry_count}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn builds_and_validates() {
        assert_eq!(idempotency_key("ATT-0042", 0).unwrap(), "ATT-0042-r0");
        assert_eq!(idempotency_key("SUB-7", 3).unwrap(), "SUB-7-r3");
        assert!(idempotency_key("", 0).is_err());
        assert!(idempotency_key("ATT-1", -1).is_err());
    }
}
