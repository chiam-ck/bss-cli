//! Per-customer chat caps — hourly rate + monthly cost. Port of
//! `orchestrator/bss_orchestrator/chat_caps.py` (v0.12 PR5).
//!
//! Two caps, two storage shapes:
//!
//! * **Hourly rate cap** — in-memory sliding window of recent request
//!   timestamps per customer. Single-process; abstracted behind [`HourlyWindow`]
//!   so a later version can swap to Redis if scale demands.
//! * **Monthly cost cap** — DB-backed via `audit.chat_usage`. One row per
//!   (customer_id, period_yyyymm); cost rolled up from the OpenRouter response's
//!   token counts × per-model rate.
//!
//! **Doctrine: fail closed.** If the cap check can't complete (DB unreachable,
//! etc.) [`ChatCaps::check_caps`] returns `allowed=false` with reason
//! `cap_check_failed` — a cap that doesn't enforce is worse than no cap.
//!
//! **Port note — the pool is injected, not lazily self-created.** Python builds
//! its own `AsyncEngine` (pool_size=2) behind a module-global lazy init because
//! the orchestrator library has no handle on the portal's engine. In-process in
//! Rust the portal already owns a `PgPool` against the same Postgres, so it is
//! passed to [`ChatCaps::new`]. Same database, same SQL, same semantics — only
//! the connection provenance differs. `None` (no `BSS_DB_URL`) makes the monthly
//! read fail closed, matching Python's `RuntimeError` from `_get_engine`.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use chrono::{DateTime, Datelike, Duration, TimeZone, Utc};
use sqlx::{PgPool, Row};

// ─── Per-model OpenRouter rates ──────────────────────────────────────

/// Sourced from openrouter.ai/models, in USD per 1M tokens (input, output).
/// Approximate; updated when the headline model swaps. Rate accuracy is OK for
/// cap enforcement — the goal is to bound a runaway customer at ~$2/month, not
/// to bill perfectly.
pub const MODEL_RATES_USD_PER_M_TOK: &[(&str, (f64, f64))] = &[
    ("google/gemma-4-26b-a4b-it", (0.15, 0.50)),
    // v1.5.1 — placeholder rates for the new default; verify against
    // openrouter.ai/models headline pricing on next billing review.
    // Slightly-too-high is the safe direction (cap enforcement, not billing).
    ("deepseek/deepseek-v4-pro", (1.00, 4.00)),
];

/// v1.5.1 — conservative fallback used when (a) the request's model isn't in
/// [`MODEL_RATES_USD_PER_M_TOK`] AND (b) the configured `llm_model` isn't
/// either. Pre-v1.5.1 the fallback recursed onto `settings.llm_model` and
/// raised `KeyError` when that was also unknown, breaking cap accounting on any
/// unconfigured swap.
pub const FALLBACK_RATE: (f64, f64) = (2.00, 8.00);

fn rate_for(model: &str) -> Option<(f64, f64)> {
    MODEL_RATES_USD_PER_M_TOK
        .iter()
        .find(|(m, _)| *m == model)
        .map(|(_, r)| *r)
}

/// Convert token counts to integer cents. Unknown model → falls back to the
/// configured headline model's rate so we never under-count; logs a warning so
/// the rate table can be updated. If even the configured headline isn't in the
/// table, falls back to the conservative [`FALLBACK_RATE`] ceiling — caps still
/// enforce correctly, just with extra headroom for the operator to notice the
/// missing entry.
///
/// Pure — the cap arithmetic is golden-tested against the Python oracle.
pub fn cost_cents_for_turn(
    model: &str,
    configured_model: &str,
    prompt_tok: i64,
    completion_tok: i64,
) -> i64 {
    let rates = match rate_for(model) {
        Some(r) => r,
        None => {
            tracing::warn!(model = %model, "chat_caps.unknown_model");
            match rate_for(configured_model) {
                Some(r) => r,
                None => {
                    tracing::warn!(
                        configured_model = %configured_model,
                        using_fallback_rate = ?FALLBACK_RATE,
                        "chat_caps.configured_model_also_unknown"
                    );
                    FALLBACK_RATE
                }
            }
        }
    };
    let (in_rate, out_rate) = rates;
    let usd = (prompt_tok as f64 / 1_000_000.0) * in_rate
        + (completion_tok as f64 / 1_000_000.0) * out_rate;
    // Round up so partial cents always count toward the cap. Python:
    // `max(0, int(usd * 100 + 0.999))` — `int()` truncates toward zero, which
    // `as i64` matches for the non-negative values this can produce.
    ((usd * 100.0 + 0.999) as i64).max(0)
}

// ─── CapStatus ───────────────────────────────────────────────────────

/// Result of [`ChatCaps::check_caps`]. The chat route inspects `allowed` first;
/// on false it consumes `reason` (`hourly_rate_cap` / `monthly_cost_cap` /
/// `cap_check_failed`) and `retry_at` (when the customer can try again).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapStatus {
    pub allowed: bool,
    pub reason: Option<String>,
    pub retry_at: Option<DateTime<Utc>>,
}

impl CapStatus {
    pub fn allowed() -> Self {
        Self {
            allowed: true,
            reason: None,
            retry_at: None,
        }
    }

    pub fn blocked(reason: &str, retry_at: Option<DateTime<Utc>>) -> Self {
        Self {
            allowed: false,
            reason: Some(reason.to_string()),
            retry_at,
        }
    }
}

// ─── Hourly in-memory sliding window ─────────────────────────────────

/// Single-process sliding-window counter keyed by customer_id.
///
/// The critical sections are short and non-async, so a `std::sync::Mutex` stands
/// in for Python's `asyncio.Lock`. Single-process only — a later version
/// replaces this with a memcached/Redis abstraction when horizontal scale
/// demands.
pub struct HourlyWindow {
    window: Duration,
    buckets: Mutex<HashMap<String, VecDeque<DateTime<Utc>>>>,
}

impl HourlyWindow {
    pub fn new(window: Duration) -> Self {
        Self {
            window,
            buckets: Mutex::new(HashMap::new()),
        }
    }

    /// Append `when` to `key`'s bucket, prune, and return the resulting count.
    pub fn record(&self, key: &str, when: DateTime<Utc>) -> usize {
        let mut buckets = self.lock();
        let bucket = buckets.entry(key.to_string()).or_default();
        bucket.push_back(when);
        Self::prune(bucket, when, self.window);
        bucket.len()
    }

    /// Count `key`'s live entries as of `now` (pruning expired ones).
    pub fn count(&self, key: &str, now: DateTime<Utc>) -> usize {
        let mut buckets = self.lock();
        match buckets.get_mut(key) {
            None => 0,
            Some(bucket) => {
                if bucket.is_empty() {
                    return 0;
                }
                Self::prune(bucket, now, self.window);
                bucket.len()
            }
        }
    }

    /// Drop one key's window, or all of them when `key` is `None`. Test hook —
    /// mirrors Python's `_HourlyWindow.reset`.
    pub fn reset(&self, key: Option<&str>) {
        let mut buckets = self.lock();
        match key {
            None => buckets.clear(),
            Some(k) => {
                buckets.remove(k);
            }
        }
    }

    fn prune(bucket: &mut VecDeque<DateTime<Utc>>, now: DateTime<Utc>, window: Duration) {
        let cutoff = now - window;
        while let Some(front) = bucket.front() {
            if *front < cutoff {
                bucket.pop_front();
            } else {
                break;
            }
        }
    }

    /// The buckets are a cache; a poisoned lock is recoverable.
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, VecDeque<DateTime<Utc>>>> {
        self.buckets.lock().unwrap_or_else(|e| e.into_inner())
    }
}

// ─── Period helpers ──────────────────────────────────────────────────

/// `YYYYMM` as an integer — the `audit.chat_usage` partition key.
pub fn period_yyyymm(when: DateTime<Utc>) -> i32 {
    when.year() * 100 + when.month() as i32
}

/// Midnight on the first of the month following `now` — the monthly-cap
/// `retry_at`. December rolls into January of the next year.
fn next_period_start(now: DateTime<Utc>) -> DateTime<Utc> {
    let (year, month) = if now.month() == 12 {
        (now.year() + 1, 1)
    } else {
        (now.year(), now.month() + 1)
    };
    Utc.with_ymd_and_hms(year, month, 1, 0, 0, 0)
        .single()
        .unwrap_or(now)
}

// ─── The pure decision ───────────────────────────────────────────────

/// Caps configuration — the three `BSS_CHAT_*` settings.
#[derive(Debug, Clone, Copy)]
pub struct CapLimits {
    pub rate_per_customer_per_hour: i64,
    pub cost_cap_per_customer_per_month_cents: i64,
    pub rate_per_ip_per_hour: i64,
}

impl Default for CapLimits {
    fn default() -> Self {
        Self {
            rate_per_customer_per_hour: 20,
            cost_cap_per_customer_per_month_cents: 200,
            rate_per_ip_per_hour: 60,
        }
    }
}

/// The cap decision, factored out of the IO so the rules are unit-testable
/// without a database. Hourly is checked first (cheap, in-memory), matching the
/// Python ordering — a customer over the hourly cap never triggers the DB read.
pub fn decide(
    hourly_count: i64,
    month_cost_cents: i64,
    limits: &CapLimits,
    now: DateTime<Utc>,
) -> CapStatus {
    if hourly_count >= limits.rate_per_customer_per_hour {
        return CapStatus::blocked("hourly_rate_cap", Some(now + Duration::hours(1)));
    }
    if month_cost_cents >= limits.cost_cap_per_customer_per_month_cents {
        return CapStatus::blocked("monthly_cost_cap", Some(next_period_start(now)));
    }
    CapStatus::allowed()
}

// ─── Public API ──────────────────────────────────────────────────────

/// Chat cap enforcement. One instance per process (the hourly windows are the
/// process-wide sliding state that Python holds in module globals).
pub struct ChatCaps {
    per_customer: HourlyWindow,
    per_ip: HourlyWindow,
    pool: Option<PgPool>,
    limits: CapLimits,
    configured_model: String,
}

impl ChatCaps {
    pub fn new(pool: Option<PgPool>, limits: CapLimits, configured_model: String) -> Self {
        Self {
            per_customer: HourlyWindow::new(Duration::hours(1)),
            per_ip: HourlyWindow::new(Duration::hours(1)),
            pool,
            limits,
            configured_model,
        }
    }

    /// Return the cap verdict for `customer_id`. **Never fails** — any error
    /// (no pool, DB unreachable) converts to
    /// `CapStatus::blocked("cap_check_failed", None)` so the chat route always
    /// refuses on uncertainty (fail closed).
    pub async fn check_caps(&self, customer_id: &str, now: DateTime<Utc>) -> CapStatus {
        let hourly = self.per_customer.count(customer_id, now) as i64;
        // Short-circuit before the DB read, matching Python's ordering.
        if hourly >= self.limits.rate_per_customer_per_hour {
            return decide(hourly, 0, &self.limits, now);
        }
        match self
            .read_month_cost_cents(customer_id, period_yyyymm(now))
            .await
        {
            Ok(cost) => decide(hourly, cost, &self.limits, now),
            Err(e) => {
                tracing::error!(
                    customer_id = %customer_id,
                    error = %e,
                    "chat_caps.check_failed"
                );
                CapStatus::blocked("cap_check_failed", None)
            }
        }
    }

    /// Increment the customer's hourly counter and monthly cost row.
    ///
    /// Two writes: the in-memory sliding window (cannot fail), then the
    /// `audit.chat_usage` atomic upsert. DB errors are logged and swallowed — a
    /// missed accounting row is cheaper than a chat that errors after the LLM
    /// already responded.
    pub async fn record_chat_turn(
        &self,
        customer_id: &str,
        prompt_tok: i64,
        completion_tok: i64,
        model: Option<&str>,
        now: DateTime<Utc>,
    ) {
        let model = model
            .filter(|m| !m.is_empty())
            .unwrap_or(&self.configured_model);
        let cost_cents =
            cost_cents_for_turn(model, &self.configured_model, prompt_tok, completion_tok);
        let period = period_yyyymm(now);

        self.per_customer.record(customer_id, now);

        if let Err(e) = self
            .upsert_usage(customer_id, period, cost_cents, now)
            .await
        {
            tracing::error!(
                customer_id = %customer_id,
                cost_cents = cost_cents,
                period = period,
                error = %e,
                "chat_caps.record_failed"
            );
        }
    }

    /// Loose per-IP rate cap. Returns true while the IP is under its hourly
    /// ceiling, false once tripped.
    ///
    /// The per-customer cap is the real gate; this exists so a pre-login
    /// attacker (or a customer hopping between accounts) can't burn the monthly
    /// cost cap on every account by spamming requests.
    ///
    /// **Port note:** no caller in the Python oracle either — `record_ip_request`
    /// is defined and tested-adjacent but never invoked by the chat route.
    /// Ported for parity so a future caller finds it, matching the treatment of
    /// the other vestigial oracle branches in this port.
    pub fn record_ip_request(&self, ip: &str, now: DateTime<Utc>) -> bool {
        let count = self.per_ip.record(ip, now) as i64;
        count <= self.limits.rate_per_ip_per_hour
    }

    async fn read_month_cost_cents(&self, customer_id: &str, period: i32) -> Result<i64, String> {
        let pool = self.pool.as_ref().ok_or_else(|| {
            "chat_caps requires BSS_DB_URL to be set — the chat surface \
             writes audit.chat_usage rows directly."
                .to_string()
        })?;
        let row = sqlx::query(
            "SELECT cost_cents FROM audit.chat_usage \
             WHERE customer_id = $1 AND period_yyyymm = $2",
        )
        .bind(customer_id)
        .bind(period)
        .fetch_optional(pool)
        .await
        .map_err(|e| e.to_string())?;
        Ok(row
            .map(|r| r.get::<i32, _>("cost_cents") as i64)
            .unwrap_or(0))
    }

    async fn upsert_usage(
        &self,
        customer_id: &str,
        period: i32,
        cost_cents: i64,
        now: DateTime<Utc>,
    ) -> Result<(), String> {
        let pool = self
            .pool
            .as_ref()
            .ok_or_else(|| "chat_caps has no database pool".to_string())?;
        sqlx::query(
            "INSERT INTO audit.chat_usage \
                 (customer_id, period_yyyymm, requests_count, cost_cents, last_updated) \
             VALUES ($1, $2, 1, $3, $4) \
             ON CONFLICT (customer_id, period_yyyymm) DO UPDATE \
                 SET requests_count = audit.chat_usage.requests_count + 1, \
                     cost_cents = audit.chat_usage.cost_cents + $3, \
                     last_updated = $4",
        )
        .bind(customer_id)
        .bind(period)
        .bind(cost_cents as i32)
        .bind(now)
        .execute(pool)
        .await
        .map_err(|e| e.to_string())?;
        Ok(())
    }

    /// Test hook — clears the in-memory windows. Mirrors the `_reset_window`
    /// autouse fixture in the Python test module.
    pub fn reset_windows(&self) {
        self.per_customer.reset(None);
        self.per_ip.reset(None);
    }

    /// Test/introspection hook — the customer's current hourly count.
    pub fn hourly_count(&self, customer_id: &str, now: DateTime<Utc>) -> usize {
        self.per_customer.count(customer_id, now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GEMMA: &str = "google/gemma-4-26b-a4b-it";
    const DEEPSEEK: &str = "deepseek/deepseek-v4-pro";

    fn t0() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 4, 27, 12, 0, 0).unwrap()
    }

    // ─── 1. Cost accounting (golden vs the Python oracle) ────────────

    #[test]
    fn known_model_cost_uses_rate_table() {
        // 0.15 + 0.50 = 0.65 USD = 65 cents.
        let cents = cost_cents_for_turn(GEMMA, DEEPSEEK, 1_000_000, 1_000_000);
        assert_eq!(cents, 65);
    }

    #[test]
    fn unknown_model_falls_back_to_configured_rate() {
        // Configured model IS in the table → its rate is used (1.00 in).
        let cents = cost_cents_for_turn("some/uncatalogued-model", DEEPSEEK, 1_000_000, 0);
        assert_eq!(cents, 100);
    }

    #[test]
    fn unknown_model_and_unknown_configured_uses_fallback_ceiling() {
        // Neither in the table → conservative (2.00, 8.00) ceiling.
        let cents = cost_cents_for_turn("a/b", "c/d", 1_000_000, 1_000_000);
        assert_eq!(cents, 1000);
    }

    #[test]
    fn zero_tokens_yields_zero_cents() {
        assert_eq!(cost_cents_for_turn(GEMMA, DEEPSEEK, 0, 0), 0);
    }

    #[test]
    fn partial_cents_always_round_up() {
        // Doctrine: any positive cost rounds up to 1 cent so nano-turns always
        // count toward the cap. Under-counting is the only failure mode that
        // matters — the cap is a soft ceiling, not a bill.
        assert_eq!(cost_cents_for_turn(GEMMA, DEEPSEEK, 100, 100), 1);
    }

    // ─── 2. Sliding window ───────────────────────────────────────────

    #[test]
    fn hourly_window_records_and_counts() {
        let win = HourlyWindow::new(Duration::hours(1));
        assert_eq!(win.count("CUST-1", t0()), 0);
        assert_eq!(win.record("CUST-1", t0()), 1);
        assert_eq!(win.record("CUST-1", t0() + Duration::minutes(10)), 2);
        assert_eq!(win.count("CUST-1", t0() + Duration::minutes(10)), 2);
    }

    #[test]
    fn hourly_window_prunes_old_entries() {
        let win = HourlyWindow::new(Duration::hours(1));
        win.record("CUST-1", t0());
        // 70 minutes later — the original entry is outside the window.
        assert_eq!(win.count("CUST-1", t0() + Duration::minutes(70)), 0);
    }

    #[test]
    fn hourly_window_isolates_customers() {
        let win = HourlyWindow::new(Duration::hours(1));
        win.record("CUST-1", t0());
        win.record("CUST-2", t0());
        assert_eq!(win.count("CUST-1", t0()), 1);
        assert_eq!(win.count("CUST-2", t0()), 1);
    }

    #[test]
    fn hourly_window_reset() {
        let win = HourlyWindow::new(Duration::hours(1));
        win.record("CUST-1", t0());
        win.record("CUST-2", t0());
        win.reset(Some("CUST-1"));
        assert_eq!(win.count("CUST-1", t0()), 0);
        assert_eq!(win.count("CUST-2", t0()), 1);
        win.reset(None);
        assert_eq!(win.count("CUST-2", t0()), 0);
    }

    // ─── 3. The cap decision ─────────────────────────────────────────

    #[test]
    fn allows_fresh_customer() {
        assert_eq!(
            decide(0, 0, &CapLimits::default(), t0()),
            CapStatus::allowed()
        );
    }

    #[test]
    fn blocks_when_hourly_rate_exceeded() {
        let s = decide(20, 0, &CapLimits::default(), t0());
        assert!(!s.allowed);
        assert_eq!(s.reason.as_deref(), Some("hourly_rate_cap"));
        assert_eq!(s.retry_at, Some(t0() + Duration::hours(1)));
    }

    #[test]
    fn blocks_when_monthly_cost_exceeded() {
        let s = decide(0, 200, &CapLimits::default(), t0());
        assert!(!s.allowed);
        assert_eq!(s.reason.as_deref(), Some("monthly_cost_cap"));
        // retry_at is the start of the following month.
        assert_eq!(
            s.retry_at,
            Some(Utc.with_ymd_and_hms(2026, 5, 1, 0, 0, 0).unwrap())
        );
    }

    #[test]
    fn handles_year_rollover() {
        let dec = Utc.with_ymd_and_hms(2026, 12, 15, 12, 0, 0).unwrap();
        let s = decide(0, 200, &CapLimits::default(), dec);
        assert_eq!(
            s.retry_at,
            Some(Utc.with_ymd_and_hms(2027, 1, 1, 0, 0, 0).unwrap())
        );
    }

    #[test]
    fn hourly_cap_wins_over_monthly_when_both_tripped() {
        // Python checks hourly first and returns immediately.
        let s = decide(20, 500, &CapLimits::default(), t0());
        assert_eq!(s.reason.as_deref(), Some("hourly_rate_cap"));
    }

    // ─── 4. Fail-closed + record ─────────────────────────────────────

    #[tokio::test]
    async fn check_caps_fails_closed_without_a_pool() {
        // Doctrine: a cap that doesn't enforce is worse than no cap. No pool →
        // the monthly read errors → blocked, never allowed.
        let caps = ChatCaps::new(None, CapLimits::default(), DEEPSEEK.to_string());
        let s = caps.check_caps("CUST-X", t0()).await;
        assert!(!s.allowed);
        assert_eq!(s.reason.as_deref(), Some("cap_check_failed"));
    }

    #[tokio::test]
    async fn check_caps_hourly_trip_needs_no_db() {
        // The hourly cap short-circuits before the DB read, so it reports the
        // real reason even with no pool at all.
        let caps = ChatCaps::new(None, CapLimits::default(), DEEPSEEK.to_string());
        for i in 0..20 {
            caps.per_customer
                .record("CUST-HOT", t0() - Duration::minutes(i));
        }
        let s = caps.check_caps("CUST-HOT", t0()).await;
        assert!(!s.allowed);
        assert_eq!(s.reason.as_deref(), Some("hourly_rate_cap"));
    }

    #[tokio::test]
    async fn record_chat_turn_swallows_db_errors_but_still_counts() {
        // A failed accounting write must not error a successful chat turn after
        // the LLM already responded — and the in-memory window (which cannot
        // fail) still records it, so the rate cap stays honest.
        let caps = ChatCaps::new(None, CapLimits::default(), DEEPSEEK.to_string());
        caps.record_chat_turn("CUST-X", 100, 200, None, t0()).await;
        assert_eq!(caps.hourly_count("CUST-X", t0()), 1);
    }

    // ─── 5. Period helpers ───────────────────────────────────────────

    #[test]
    fn period_yyyymm_format() {
        assert_eq!(
            period_yyyymm(Utc.with_ymd_and_hms(2026, 4, 27, 0, 0, 0).unwrap()),
            202604
        );
        assert_eq!(
            period_yyyymm(Utc.with_ymd_and_hms(2027, 1, 1, 0, 0, 0).unwrap()),
            202701
        );
        assert_eq!(
            period_yyyymm(Utc.with_ymd_and_hms(2026, 12, 31, 0, 0, 0).unwrap()),
            202612
        );
    }
}
