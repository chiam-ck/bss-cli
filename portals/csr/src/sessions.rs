//! Sessions-index view logic — time bucketing, humanised timestamps, and title
//! derivation. Port of the `_group_sessions_for_index` helper family in
//! `bss_csr.routes.cockpit`.
//!
//! Pure (the clock and the resolved names are injected), so the bucketing rules
//! are testable without a DB or CRM.

use chrono::{DateTime, Datelike, Duration, Timelike, Utc};
use serde::Serialize;

/// One row in the index, after name/title resolution.
#[derive(Debug, Serialize, PartialEq)]
pub struct SessionRow {
    pub session_id: String,
    pub title: String,
    pub focus_label: Option<String>,
    pub last_active_human: String,
    pub message_count: i64,
}

/// A time bucket with its rows. Empty buckets are dropped.
#[derive(Debug, Serialize, PartialEq)]
pub struct SessionBucket {
    pub label: &'static str,
    pub rows: Vec<SessionRow>,
}

/// The four buckets, in display order.
const BUCKETS: [&str; 4] = ["Today", "Yesterday", "Earlier this week", "Older"];

/// Which bucket `last_active` falls into, relative to `now`.
///
/// Note the boundaries are **midnight-anchored**, not rolling 24h windows:
/// "Yesterday" means calendar-yesterday, and "Earlier this week" is the 7 days
/// before today's midnight — so a session from 8 days ago is "Older" even though
/// it's inside a rolling week.
pub fn bucket_for(last_active: DateTime<Utc>, now: DateTime<Utc>) -> &'static str {
    let today_start = midnight(now);
    let yesterday_start = today_start - Duration::days(1);
    let week_start = today_start - Duration::days(7);
    if last_active >= today_start {
        "Today"
    } else if last_active >= yesterday_start {
        "Yesterday"
    } else if last_active >= week_start {
        "Earlier this week"
    } else {
        "Older"
    }
}

fn midnight(t: DateTime<Utc>) -> DateTime<Utc> {
    t.with_hour(0)
        .and_then(|t| t.with_minute(0))
        .and_then(|t| t.with_second(0))
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(t)
}

/// Compact human time: `14:32`, `yesterday 09:15`, `Apr 23 17:40`.
pub fn humanize_time(when: DateTime<Utc>, now: DateTime<Utc>) -> String {
    let today_start = midnight(now);
    let yesterday_start = today_start - Duration::days(1);
    if when >= today_start {
        return format!("{:02}:{:02}", when.hour(), when.minute());
    }
    if when >= yesterday_start {
        return format!("yesterday {:02}:{:02}", when.hour(), when.minute());
    }
    // Python's `%b %d` — abbreviated month + ZERO-PADDED day ("Apr 03").
    format!(
        "{} {:02} {:02}:{:02}",
        month_abbr(when.month()),
        when.day(),
        when.hour(),
        when.minute()
    )
}

fn month_abbr(m: u32) -> &'static str {
    const M: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    M[((m as usize).clamp(1, 12)) - 1]
}

/// The first user message in a transcript, trimmed for the sessions list.
///
/// Falls back to the operator-provided label, then to `(empty conversation)`.
/// Parses the cockpit `transcript_text()` format (blank-line-separated blocks,
/// each `role:\nbody`) rather than reaching into the message table — a future
/// reshape of the store must not break this path.
pub fn first_user_message_title(transcript: &str, fallback: Option<&str>) -> String {
    let fb = || {
        fallback
            .filter(|f| !f.is_empty())
            .unwrap_or("(empty conversation)")
            .to_string()
    };
    for block in transcript.split("\n\n") {
        let block = block.trim();
        if block.is_empty() {
            continue;
        }
        let (head, body) = match block.split_once('\n') {
            Some((h, b)) => (h, b),
            // `partition` yields ("head", "", "") when there's no newline, so a
            // headerless block has an empty body and can never be the title.
            None => (block, ""),
        };
        if head.trim().trim_end_matches(':') == "user" {
            let text = body.trim().split('\n').next().unwrap_or("").trim();
            if text.is_empty() {
                return fb();
            }
            // Python: `text[:77] + "…"` when len > 80 — char-wise.
            if text.chars().count() > 80 {
                let head: String = text.chars().take(77).collect();
                return format!("{head}…");
            }
            return text.to_string();
        }
    }
    fb()
}

/// Group resolved rows into the display buckets, dropping empty ones. Input order
/// is preserved within a bucket (the store returns newest-first).
pub fn group_rows(
    rows: Vec<(SessionRow, DateTime<Utc>)>,
    now: DateTime<Utc>,
) -> Vec<SessionBucket> {
    let mut buckets: Vec<(&'static str, Vec<SessionRow>)> =
        BUCKETS.iter().map(|l| (*l, Vec::new())).collect();
    for (row, last_active) in rows {
        let label = bucket_for(last_active, now);
        if let Some(slot) = buckets.iter_mut().find(|(l, _)| *l == label) {
            slot.1.push(row);
        }
    }
    buckets
        .into_iter()
        .filter(|(_, rows)| !rows.is_empty())
        .map(|(label, rows)| SessionBucket { label, rows })
        .collect()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use chrono::TimeZone;

    fn now() -> DateTime<Utc> {
        // Mid-afternoon so "today" has room on both sides.
        Utc.with_ymd_and_hms(2026, 7, 15, 14, 30, 0).unwrap()
    }

    #[test]
    fn buckets_are_midnight_anchored_not_rolling() {
        let n = now();
        // Earlier today, including 00:00 exactly.
        assert_eq!(bucket_for(n, n), "Today");
        assert_eq!(
            bucket_for(Utc.with_ymd_and_hms(2026, 7, 15, 0, 0, 0).unwrap(), n),
            "Today"
        );
        // 23:59 yesterday is Yesterday even though it's <24h ago.
        assert_eq!(
            bucket_for(Utc.with_ymd_and_hms(2026, 7, 14, 23, 59, 0).unwrap(), n),
            "Yesterday"
        );
        assert_eq!(
            bucket_for(Utc.with_ymd_and_hms(2026, 7, 14, 0, 0, 0).unwrap(), n),
            "Yesterday"
        );
        // The 7 days before today's midnight.
        assert_eq!(
            bucket_for(Utc.with_ymd_and_hms(2026, 7, 13, 12, 0, 0).unwrap(), n),
            "Earlier this week"
        );
        assert_eq!(
            bucket_for(Utc.with_ymd_and_hms(2026, 7, 8, 0, 0, 0).unwrap(), n),
            "Earlier this week"
        );
        // 8 days back falls out of the week bucket.
        assert_eq!(
            bucket_for(Utc.with_ymd_and_hms(2026, 7, 7, 23, 59, 0).unwrap(), n),
            "Older"
        );
    }

    #[test]
    fn humanize_time_shapes() {
        let n = now();
        assert_eq!(
            humanize_time(Utc.with_ymd_and_hms(2026, 7, 15, 9, 5, 0).unwrap(), n),
            "09:05"
        );
        assert_eq!(
            humanize_time(Utc.with_ymd_and_hms(2026, 7, 14, 9, 15, 0).unwrap(), n),
            "yesterday 09:15"
        );
        // %b %d — zero-padded day.
        assert_eq!(
            humanize_time(Utc.with_ymd_and_hms(2026, 4, 3, 17, 40, 0).unwrap(), n),
            "Apr 03 17:40"
        );
        assert_eq!(
            humanize_time(Utc.with_ymd_and_hms(2026, 4, 23, 17, 40, 0).unwrap(), n),
            "Apr 23 17:40"
        );
    }

    #[test]
    fn title_reads_the_first_user_block() {
        let t = "user:\nwhat's my balance?\n\nassistant:\nHere you go.\n";
        assert_eq!(first_user_message_title(t, None), "what's my balance?");
    }

    #[test]
    fn title_skips_non_user_blocks() {
        // An assistant-first transcript still finds the user's line.
        let t = "assistant:\nHi!\n\nuser:\nhello there\n";
        assert_eq!(first_user_message_title(t, None), "hello there");
    }

    #[test]
    fn title_takes_only_the_first_line_of_the_block() {
        let t = "user:\nline one\nline two\n";
        assert_eq!(first_user_message_title(t, None), "line one");
    }

    #[test]
    fn title_truncates_at_80_chars_with_an_ellipsis() {
        let long = "x".repeat(100);
        let t = format!("user:\n{long}\n");
        let got = first_user_message_title(&t, None);
        assert_eq!(got.chars().count(), 78, "77 chars + the ellipsis");
        assert!(got.ends_with('…'));
        // Exactly 80 is NOT truncated (Python: `if len(text) > 80`).
        let exact = "y".repeat(80);
        let t = format!("user:\n{exact}\n");
        assert_eq!(first_user_message_title(&t, None), exact);
    }

    #[test]
    fn title_falls_back_when_no_user_block() {
        assert_eq!(
            first_user_message_title("assistant:\nHi\n", Some("my label")),
            "my label"
        );
        assert_eq!(
            first_user_message_title("assistant:\nHi\n", None),
            "(empty conversation)"
        );
        assert_eq!(first_user_message_title("", None), "(empty conversation)");
    }

    #[test]
    fn group_drops_empty_buckets_and_keeps_order() {
        let n = now();
        let mk = |id: &str| SessionRow {
            session_id: id.to_string(),
            title: "t".to_string(),
            focus_label: None,
            last_active_human: "x".to_string(),
            message_count: 1,
        };
        let out = group_rows(
            vec![
                (mk("a"), n),
                (mk("b"), Utc.with_ymd_and_hms(2026, 7, 1, 0, 0, 0).unwrap()),
                (mk("c"), n),
            ],
            n,
        );
        // Yesterday + Earlier-this-week are empty and dropped entirely.
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].label, "Today");
        assert_eq!(out[1].label, "Older");
        // Input order preserved within a bucket.
        assert_eq!(out[0].rows[0].session_id, "a");
        assert_eq!(out[0].rows[1].session_id, "c");
    }
}
