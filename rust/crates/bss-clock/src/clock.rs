//! Process-local scenario clock.
//!
//! Two modes:
//!
//! - [`Mode::Wall`] — return wall-clock UTC plus an optional `offset` that
//!   [`advance`] can shift. In this mode [`now`] keeps ticking.
//! - [`Mode::Frozen`] — return a fixed instant forever until [`unfreeze`] or
//!   [`advance`] is called. `advance` on a frozen clock shifts the frozen
//!   instant forward (it does *not* resume wall-clock ticking).
//!
//! State is process-global (an [`ArcSwap`] so [`now`] reads are lock-free —
//! see phases/2.0/02-TECH-MAPPING.md §2.2). Each service process has one
//! clock. Tests use [`reset_for_tests`] to reset state between cases.

use std::sync::{Arc, LazyLock};

use arc_swap::ArcSwap;
use chrono::{DateTime, Duration, Utc};

/// Clock mode. Serialises to the strings `"wall"` / `"frozen"` (matching the
/// Python `Literal["wall", "frozen"]` on the wire).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Wall,
    Frozen,
}

impl Mode {
    /// Wire string, matching the Python `_Mode` literal values.
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Wall => "wall",
            Mode::Frozen => "frozen",
        }
    }
}

/// Internal, mutable clock state. Held behind [`ArcSwap`]; readers clone the
/// current `Arc`, writers publish a fresh one via `rcu`.
#[derive(Clone)]
struct Inner {
    mode: Mode,
    frozen_at: Option<DateTime<Utc>>,
    /// Positive offset added to wall clock in `Wall` mode. Shifted by
    /// [`advance`] calls while not frozen.
    offset: Duration,
}

impl Default for Inner {
    fn default() -> Self {
        Inner {
            mode: Mode::Wall,
            frozen_at: None,
            offset: Duration::zero(),
        }
    }
}

static STATE: LazyLock<ArcSwap<Inner>> = LazyLock::new(|| ArcSwap::from_pointee(Inner::default()));

fn compute_now(inner: &Inner) -> DateTime<Utc> {
    match inner.mode {
        // `frozen_at` is always `Some` when mode is `Frozen` (invariant upheld
        // by every writer below); fall back to wall time rather than panic.
        Mode::Frozen => inner.frozen_at.unwrap_or_else(Utc::now),
        Mode::Wall => Utc::now() + inner.offset,
    }
}

/// Return the current time as a UTC datetime.
///
/// Use this everywhere instead of `Utc::now()`. In production it's equivalent
/// to wall-clock UTC; during scenarios it reflects whatever freeze/advance
/// commands the runner has issued.
pub fn now() -> DateTime<Utc> {
    compute_now(&STATE.load())
}

/// Freeze the clock at `at` (or the current [`now`] when `None`).
///
/// Returns the instant the clock was frozen at. Re-calling `freeze` while
/// already frozen shifts the frozen instant to the new value.
pub fn freeze(at: Option<DateTime<Utc>>) -> DateTime<Utc> {
    let pinned = at.unwrap_or_else(now);
    STATE.rcu(|cur| Inner {
        mode: Mode::Frozen,
        frozen_at: Some(pinned),
        offset: cur.offset,
    });
    pinned
}

/// Resume wall-clock ticking (keeps any accumulated offset).
///
/// After `unfreeze` the clock returns wall-clock + current offset. Callers that
/// want a full reset should use [`reset_for_tests`].
pub fn unfreeze() {
    STATE.rcu(|cur| Inner {
        mode: Mode::Wall,
        frozen_at: None,
        offset: cur.offset,
    });
}

/// Errors from clock operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClockError {
    /// `advance` was given a negative duration.
    NegativeDuration,
    /// [`parse_duration`] could not parse the input; carries the raw string.
    InvalidDuration(String),
}

impl std::fmt::Display for ClockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClockError::NegativeDuration => {
                write!(f, "advance requires a non-negative duration")
            }
            ClockError::InvalidDuration(v) => write!(
                f,
                "invalid duration {v:?} — expected '<N><s|m|h|d>' (e.g. '30d')"
            ),
        }
    }
}

impl std::error::Error for ClockError {}

/// Shift the clock forward by `delta`.
///
/// When frozen, advances the frozen instant; when unfrozen, accumulates into
/// the offset. Returns the new [`now`]. Rejects negative durations.
pub fn advance(delta: Duration) -> Result<DateTime<Utc>, ClockError> {
    if delta < Duration::zero() {
        return Err(ClockError::NegativeDuration);
    }
    STATE.rcu(|cur| match cur.mode {
        Mode::Frozen => Inner {
            mode: Mode::Frozen,
            frozen_at: Some(cur.frozen_at.unwrap_or_else(Utc::now) + delta),
            offset: cur.offset,
        },
        Mode::Wall => Inner {
            mode: Mode::Wall,
            frozen_at: None,
            offset: cur.offset + delta,
        },
    });
    Ok(now())
}

/// Read-only snapshot of the clock for admin/diagnostic responses.
#[derive(Debug, Clone, PartialEq)]
pub struct ClockState {
    pub mode: Mode,
    pub now: DateTime<Utc>,
    pub offset_seconds: f64,
    pub frozen_at: Option<DateTime<Utc>>,
}

/// Return a read-only snapshot of the current clock state.
pub fn state() -> ClockState {
    let inner = STATE.load();
    ClockState {
        mode: inner.mode,
        now: compute_now(&inner),
        offset_seconds: inner.offset.num_milliseconds() as f64 / 1000.0,
        frozen_at: inner.frozen_at,
    }
}

/// Restore a fresh wall-clock state — test-teardown helper.
pub fn reset_for_tests() {
    STATE.store(Arc::new(Inner::default()));
}

/// Parse `"30d"` / `"2h"` / `"15m"` / `"45s"` into a [`Duration`].
///
/// Deliberately narrow — no weeks, no compound strings — matching the Python
/// regex `^\s*(\d+)\s*([smhd])\s*$`.
pub fn parse_duration(value: &str) -> Result<Duration, ClockError> {
    let err = || ClockError::InvalidDuration(value.to_string());
    let trimmed = value.trim();
    let unit = match trimmed.chars().last() {
        Some(c @ ('s' | 'm' | 'h' | 'd')) => c,
        _ => return Err(err()),
    };
    let digits = trimmed[..trimmed.len() - unit.len_utf8()].trim_end();
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(err());
    }
    let qty: i64 = digits.parse().map_err(|_| err())?;
    Ok(match unit {
        's' => Duration::seconds(qty),
        'm' => Duration::minutes(qty),
        'h' => Duration::hours(qty),
        'd' => Duration::days(qty),
        // Unreachable: `unit` is constrained to s/m/h/d by the match above.
        _ => return Err(err()),
    })
}
