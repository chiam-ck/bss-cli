//! The v1.6.1 disconnect-proof turn registry. Port of the `_INFLIGHT` /
//! `_pump` / `_observe` machinery in `bss_csr.routes.cockpit`.
//!
//! # Why this exists
//!
//! Cockpit turns run 20–50s with long silent stretches between SSE frames. iPad
//! Safari (and assorted middleboxes) kill quiet streams, and before v1.6.1 the
//! agent ran **inside** the response generator — a dropped connection cancelled
//! the turn mid-flight with nothing persisted. The observable symptom: the first
//! message "never answers", or only answers after the next turn re-drives it.
//!
//! So the turn runs in a **detached task** that persists its results no matter
//! what the socket does; the SSE response merely forwards frames from a channel,
//! emitting heartbeats so the pipe is never idle. An EventSource reconnect while
//! the turn is still running **attaches as an observer** (no double-driving) and
//! triggers a page reload when the turn lands.
//!
//! # ⚠️ This is the OPPOSITE of the self-serve chat
//!
//! In `portals/self-serve`, a dropped receiver **cancels** the turn — the sink
//! returns `false` and `astream_once_to` bails. Here the turn **must survive** the
//! socket dying. Do not pattern-match the two: a customer's abandoned chat turn is
//! waste to be stopped; an operator's abandoned cockpit turn may already be
//! halfway through a destructive action, and must land and persist.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard};

use tokio::sync::broadcast;
use tokio::task::JoinHandle;

/// A real SSE **event**, not a comment: comments are invisible to `EventSource`,
/// so a client-side watchdog couldn't tell a healthy quiet stream from a dead
/// socket. A status re-render is cheap and observable.
pub fn heartbeat_frame() -> Vec<u8> {
    bss_portal_ui::sse::format_frame("status", &bss_portal_ui::sse::status_html("live"))
}

/// Seconds of silence before a heartbeat is emitted.
pub const HEARTBEAT_SECONDS: u64 = 10;

/// Reload marker: swapped into the stream like any message frame; the thread
/// page's `htmx:afterSwap` handler spots it and reloads.
///
/// A `<div>` marker (**not** a `<script>`) so we don't depend on Safari/htmx
/// script-eval semantics inside SSE swaps.
pub const RELOAD_FRAME_HTML: &str = "<div class=\"bss-reload\" hidden></div>";

/// One in-flight turn: the detached task plus a broadcast channel so late
/// arrivals (reconnects, a second tab) can attach without stealing frames from
/// the original consumer.
struct Turn {
    handle: JoinHandle<()>,
    frames: broadcast::Sender<Vec<u8>>,
}

/// Process-wide registry of running turns, keyed by session id.
#[derive(Clone, Default)]
pub struct Inflight {
    turns: Arc<Mutex<HashMap<String, Turn>>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl Inflight {
    pub fn new() -> Self {
        Self::default()
    }

    /// Is a turn currently running for this session?
    ///
    /// Finished tasks are reaped here rather than by a sweeper — the registry is
    /// only ever consulted on a `/events` request, so lazily dropping a completed
    /// entry is both sufficient and cheaper than a timer.
    pub fn active(&self, session_id: &str) -> bool {
        let mut turns = lock(&self.turns);
        match turns.get(session_id) {
            Some(t) if !t.handle.is_finished() => true,
            Some(_) => {
                turns.remove(session_id);
                false
            }
            None => false,
        }
    }

    /// Subscribe to a running turn's frames. `None` when nothing is running.
    pub fn observe(&self, session_id: &str) -> Option<broadcast::Receiver<Vec<u8>>> {
        let turns = lock(&self.turns);
        let t = turns.get(session_id)?;
        if t.handle.is_finished() {
            return None;
        }
        Some(t.frames.subscribe())
    }

    /// Register a freshly-spawned turn. Replaces any finished entry for the same
    /// session.
    pub fn insert(
        &self,
        session_id: &str,
        handle: JoinHandle<()>,
        frames: broadcast::Sender<Vec<u8>>,
    ) {
        lock(&self.turns).insert(session_id.to_string(), Turn { handle, frames });
    }

    /// Drop a session's entry (the turn task calls this as it completes).
    pub fn remove(&self, session_id: &str) {
        lock(&self.turns).remove(session_id);
    }

    /// Test/introspection hook.
    pub fn len(&self) -> usize {
        lock(&self.turns).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn heartbeat_is_a_real_event_not_a_comment() {
        // A `: comment` frame is invisible to EventSource — the watchdog needs an
        // observable event, so this must be a `status` event.
        let f = String::from_utf8(heartbeat_frame()).unwrap();
        assert!(f.starts_with("event: status\n"), "got {f:?}");
        assert!(f.contains("live"));
    }

    #[test]
    fn reload_marker_is_a_div_not_a_script() {
        // Safari/htmx script-eval semantics inside an SSE swap are unreliable.
        assert!(RELOAD_FRAME_HTML.starts_with("<div"));
        assert!(!RELOAD_FRAME_HTML.contains("<script"));
    }

    #[tokio::test]
    async fn a_running_turn_is_active_and_observable() {
        let reg = Inflight::new();
        let (tx, _) = broadcast::channel(16);
        let tx2 = tx.clone();
        let handle = tokio::spawn(async move {
            // Keep the task alive until the test drops the guard.
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            let _ = tx2.send(b"done".to_vec());
        });
        reg.insert("S-1", handle, tx);

        assert!(reg.active("S-1"));
        assert!(reg.observe("S-1").is_some(), "a reconnect can attach");
        assert!(!reg.active("S-2"));
        assert!(reg.observe("S-2").is_none());
    }

    #[tokio::test]
    async fn a_finished_turn_is_reaped_lazily() {
        let reg = Inflight::new();
        let (tx, _) = broadcast::channel(16);
        let handle = tokio::spawn(async {});
        reg.insert("S-1", handle, tx);
        // Let it finish.
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert_eq!(reg.len(), 1, "still registered until consulted");
        assert!(!reg.active("S-1"), "a finished turn is not active");
        assert_eq!(reg.len(), 0, "and consulting it reaps the entry");
    }

    /// Observers see the same frames — a second tab doesn't steal from the first.
    #[tokio::test]
    async fn observers_share_frames_rather_than_stealing_them() {
        let reg = Inflight::new();
        let (tx, _) = broadcast::channel(16);
        let tx2 = tx.clone();
        let handle = tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
            let _ = tx2.send(b"frame".to_vec());
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        });
        reg.insert("S-1", handle, tx);

        let mut a = reg.observe("S-1").expect("first observer");
        let mut b = reg.observe("S-1").expect("second observer");
        assert_eq!(a.recv().await.unwrap(), b"frame".to_vec());
        assert_eq!(b.recv().await.unwrap(), b"frame".to_vec());
    }

    /// The whole point of v1.6.1: dropping every observer must NOT stop the turn.
    #[tokio::test]
    async fn dropping_all_observers_does_not_cancel_the_turn() {
        let reg = Inflight::new();
        let (tx, _) = broadcast::channel(16);
        let landed = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = landed.clone();
        let handle = tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(60)).await;
            // This is the "persist the result" beat — it must happen even with
            // nobody listening (the socket died; the operator's turn still ran).
            flag.store(true, std::sync::atomic::Ordering::SeqCst);
        });
        reg.insert("S-1", handle, tx);

        // Simulate the browser going away: subscribe, then drop.
        drop(reg.observe("S-1"));

        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        assert!(
            landed.load(std::sync::atomic::Ordering::SeqCst),
            "the detached turn must persist its result even with no observers — \
             this is the v1.6.1 contract, and the OPPOSITE of the self-serve chat"
        );
    }
}
