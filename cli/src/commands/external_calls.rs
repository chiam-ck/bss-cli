//! `bss external-calls` — read-only browser over the `integrations.external_call`
//! forensic substrate (v0.14+). Port of `cli/bss_cli/commands/external_calls.py`.
//!
//! The CLI never inserts or updates `external_call` rows — that's the adapter
//! layer's job. This is the one command group that talks to Postgres directly
//! (via `bss-db`'s pool) rather than through a service HTTP client: the forensic
//! log is cross-provider triage data with no owning service surface.

use std::process::ExitCode;

use chrono::{DateTime, Datelike, Duration, Timelike, Utc};
use clap::Args;
use sqlx::Row;

#[derive(Args)]
pub struct ExternalCallsArgs {
    /// Filter by provider name.
    #[arg(long, short = 'p')]
    provider: Option<String>,
    /// Time window relative to now: 30s | 5m | 1h | 24h | 7d. Conflicts with
    /// `--month-to-date`.
    #[arg(long)]
    since: Option<String>,
    /// Filter by aggregate id (e.g. IDT-0042).
    #[arg(long, short = 'a')]
    aggregate: Option<String>,
    /// Show count for the current calendar month (free-tier monitoring).
    #[arg(long = "month-to-date")]
    month_to_date: bool,
    /// Max rows to display (default 50).
    #[arg(long, short = 'n', default_value_t = 50)]
    limit: i64,
    /// Show only success=false rows.
    #[arg(long = "failures")]
    failures_only: bool,
}

pub async fn run(args: ExternalCallsArgs) -> ExitCode {
    if args.month_to_date && args.since.is_some() {
        eprintln!("--month-to-date and --since are mutually exclusive");
        return ExitCode::from(2);
    }

    let since_dt: Option<DateTime<Utc>> = if args.month_to_date {
        Some(month_start(bss_clock::now()))
    } else if let Some(spec) = args.since.as_deref() {
        match parse_since(spec) {
            Ok(dt) => Some(dt),
            Err(()) => {
                // Python's `{spec!r}` — repr with single quotes; `{{s,m,h,d}}` → `{s,m,h,d}`.
                eprintln!(
                    "--since '{spec}' not parseable; expected '<n>{{s,m,h,d}}' \
                     e.g. '30m', '24h', '7d'"
                );
                return ExitCode::from(2);
            }
        }
    } else {
        None
    };

    let Ok(db_url) = std::env::var("BSS_DB_URL") else {
        eprintln!("BSS_DB_URL not set; cannot query external_call.");
        return ExitCode::from(2);
    };
    if db_url.is_empty() {
        eprintln!("BSS_DB_URL not set; cannot query external_call.");
        return ExitCode::from(2);
    }

    let rows = match query(&db_url, &args, since_dt).await {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("failed to query external_call: {e}");
            return ExitCode::from(1);
        }
    };

    if args.month_to_date {
        render_month_summary(&rows, args.limit);
    } else {
        render_rows(&rows);
    }
    ExitCode::SUCCESS
}

/// One `external_call` row, only the columns the CLI renders.
struct Call {
    provider: String,
    operation: String,
    aggregate_type: Option<String>,
    aggregate_id: Option<String>,
    success: bool,
    latency_ms: i32,
    provider_call_id: Option<String>,
    error_code: Option<String>,
    error_message: Option<String>,
    occurred_at: DateTime<Utc>,
}

/// `now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)` — the first
/// instant of the current calendar month, in UTC (the clock is UTC).
fn month_start(now: DateTime<Utc>) -> DateTime<Utc> {
    now.with_day(1)
        .and_then(|d| d.with_hour(0))
        .and_then(|d| d.with_minute(0))
        .and_then(|d| d.with_second(0))
        .and_then(|d| d.with_nanosecond(0))
        .unwrap_or(now)
}

/// Parse `30s` / `5m` / `1h` / `24h` / `7d` into a moment that far in the past.
/// `Err(())` ⇒ the caller owns the stderr message + exit 2. Mirrors `_DURATION_RE`.
fn parse_since(spec: &str) -> Result<DateTime<Utc>, ()> {
    let spec = spec.trim();
    let (num, unit) = spec.split_at(spec.find(|c: char| !c.is_ascii_digit()).ok_or(())?);
    if num.is_empty() || unit.len() != 1 {
        return Err(());
    }
    let n: i64 = num.parse().map_err(|_| ())?;
    let delta = match unit {
        "s" => Duration::seconds(n),
        "m" => Duration::minutes(n),
        "h" => Duration::hours(n),
        "d" => Duration::days(n),
        _ => return Err(()),
    };
    Ok(bss_clock::now() - delta)
}

async fn query(
    db_url: &str,
    args: &ExternalCallsArgs,
    since: Option<DateTime<Utc>>,
) -> Result<Vec<Call>, sqlx::Error> {
    let pool = bss_db::connect(db_url).await?;

    // Build the WHERE clause with positional placeholders in bind order.
    let mut conds: Vec<String> = Vec::new();
    let mut idx = 1;
    if args.provider.is_some() {
        conds.push(format!("provider = ${idx}"));
        idx += 1;
    }
    if since.is_some() {
        conds.push(format!("occurred_at >= ${idx}"));
        idx += 1;
    }
    if args.aggregate.is_some() {
        conds.push(format!("aggregate_id = ${idx}"));
        idx += 1;
    }
    if args.failures_only {
        // `ExternalCall.success.is_(False)` — no bind, inline like the ORM emits.
        conds.push("success = false".to_string());
    }
    let where_clause = if conds.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", conds.join(" AND "))
    };
    let sql = format!(
        "SELECT provider, operation, aggregate_type, aggregate_id, success, \
         latency_ms, provider_call_id, error_code, error_message, occurred_at \
         FROM integrations.external_call{where_clause} \
         ORDER BY occurred_at DESC LIMIT ${idx}"
    );

    let mut q = sqlx::query(&sql);
    if let Some(p) = &args.provider {
        q = q.bind(p);
    }
    if let Some(s) = since {
        q = q.bind(s);
    }
    if let Some(a) = &args.aggregate {
        q = q.bind(a);
    }
    q = q.bind(args.limit);

    let rows = q.fetch_all(&pool).await?;
    pool.close().await;

    rows.iter()
        .map(|r| {
            Ok(Call {
                provider: r.try_get("provider")?,
                operation: r.try_get("operation")?,
                aggregate_type: r.try_get("aggregate_type")?,
                aggregate_id: r.try_get("aggregate_id")?,
                success: r.try_get("success")?,
                latency_ms: r.try_get("latency_ms")?,
                provider_call_id: r.try_get("provider_call_id")?,
                error_code: r.try_get("error_code")?,
                error_message: r.try_get("error_message")?,
                occurred_at: r.try_get("occurred_at")?,
            })
        })
        .collect()
}

/// The row browser. Python renders a `rich.Table`; the box-drawing chrome is a
/// documented CLI seam — the per-row cell values match Python exactly.
fn render_rows(rows: &[Call]) {
    if rows.is_empty() {
        println!("No matching calls.");
        return;
    }
    println!("External calls (last {})", rows.len());
    println!("when            provider  op  ok  ms  aggregate  call id  error");
    for r in rows {
        let ok = if r.success { "✓" } else { "✗" };
        let when = r.occurred_at.format("%m-%d %H:%M:%S");
        let agg = if r.aggregate_type.is_some() || r.aggregate_id.is_some() {
            format!(
                "{}:{}",
                r.aggregate_type.as_deref().unwrap_or(""),
                r.aggregate_id.as_deref().unwrap_or("")
            )
        } else {
            String::new()
        };
        let err: String = r
            .error_message
            .as_deref()
            .or(r.error_code.as_deref())
            .unwrap_or("")
            .chars()
            .take(40)
            .collect();
        println!(
            "{}  {}  {}  {}  {}  {}  {}  {}",
            when,
            r.provider,
            r.operation,
            ok,
            r.latency_ms,
            agg,
            r.provider_call_id.as_deref().unwrap_or(""),
            err,
        );
    }
}

/// The `--month-to-date` aggregate-by-provider summary. Same `rich.Table` seam.
fn render_month_summary(rows: &[Call], limit: i64) {
    use std::collections::BTreeMap;
    let mut by_provider: BTreeMap<&str, u64> = BTreeMap::new();
    let mut by_provider_failed: BTreeMap<&str, u64> = BTreeMap::new();
    for r in rows {
        *by_provider.entry(&r.provider).or_insert(0) += 1;
        if !r.success {
            *by_provider_failed.entry(&r.provider).or_insert(0) += 1;
        }
    }
    if by_provider.is_empty() {
        println!("No calls this calendar month.");
        return;
    }
    println!("External calls (month to date, ≤{limit} rows scanned)");
    println!("provider  calls  failures");
    // BTreeMap iterates in sorted key order — matches Python's `sorted(by_provider)`.
    for (p, calls) in &by_provider {
        let failed = by_provider_failed.get(p).copied().unwrap_or(0);
        println!("{p}  {calls}  {failed}");
    }
}
