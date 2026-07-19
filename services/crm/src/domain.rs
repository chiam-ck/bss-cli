//! Pure domain FSMs — port of `app.domain.{case,ticket,esim,port_request}_state`.
//!
//! Four independent state machines, each `(trigger, source, dest)` tables with
//! `is_valid_transition` / `get_next_state`. No DB, no clock. Unit-tested against
//! the Python transition tables.

/// Case FSM: open/in_progress/pending_customer/resolved/closed.
pub mod case {
    const TRANSITIONS: &[(&str, &str, &str)] = &[
        ("take", "open", "in_progress"),
        ("await_customer", "in_progress", "pending_customer"),
        ("resume", "pending_customer", "in_progress"),
        ("resolve", "in_progress", "resolved"),
        ("resolve", "open", "resolved"),
        ("close", "resolved", "closed"),
        ("cancel", "open", "closed"),
        ("cancel", "in_progress", "closed"),
        ("cancel", "pending_customer", "closed"),
    ];

    pub fn is_valid_transition(from: &str, trigger: &str) -> bool {
        TRANSITIONS
            .iter()
            .any(|(t, s, _)| *t == trigger && *s == from)
    }
    pub fn get_next_state(from: &str, trigger: &str) -> Option<&'static str> {
        TRANSITIONS
            .iter()
            .find(|(t, s, _)| *t == trigger && *s == from)
            .map(|(_, _, d)| *d)
    }
}

/// Ticket FSM: open/acknowledged/in_progress/pending/resolved/closed/cancelled.
pub mod ticket {
    const TRANSITIONS: &[(&str, &str, &str)] = &[
        ("ack", "open", "acknowledged"),
        ("start", "acknowledged", "in_progress"),
        ("wait", "in_progress", "pending"),
        ("resume", "pending", "in_progress"),
        ("resolve", "in_progress", "resolved"),
        ("close", "resolved", "closed"),
        ("reopen", "resolved", "in_progress"),
        ("cancel", "open", "cancelled"),
        ("cancel", "acknowledged", "cancelled"),
        ("cancel", "in_progress", "cancelled"),
        ("cancel", "pending", "cancelled"),
    ];
    /// Non-terminal states (used by `find_open_by_case` + cancel guard).
    pub const CANCELLABLE: &[&str] = &["open", "acknowledged", "in_progress", "pending"];
    pub const TERMINAL: &[&str] = &["closed", "cancelled"];

    pub fn is_valid_transition(from: &str, trigger: &str) -> bool {
        TRANSITIONS
            .iter()
            .any(|(t, s, _)| *t == trigger && *s == from)
    }
    pub fn get_next_state(from: &str, trigger: &str) -> Option<&'static str> {
        TRANSITIONS
            .iter()
            .find(|(t, s, _)| *t == trigger && *s == from)
            .map(|(_, _, d)| *d)
    }
}

/// eSIM FSM: available/reserved/downloaded/activated/suspended/recycled.
pub mod esim {
    const TRANSITIONS: &[(&str, &str, &str)] = &[
        ("reserve", "available", "reserved"),
        ("assign_msisdn", "reserved", "reserved"),
        ("download", "reserved", "downloaded"),
        ("activate", "downloaded", "activated"),
        ("suspend", "activated", "suspended"),
        ("activate", "suspended", "activated"),
        ("recycle", "activated", "recycled"),
        ("release", "reserved", "available"),
    ];
    pub fn is_valid_transition(from: &str, trigger: &str) -> bool {
        TRANSITIONS
            .iter()
            .any(|(t, s, _)| *t == trigger && *s == from)
    }
    pub fn get_next_state(from: &str, trigger: &str) -> Option<&'static str> {
        TRANSITIONS
            .iter()
            .find(|(t, s, _)| *t == trigger && *s == from)
            .map(|(_, _, d)| *d)
    }
}

/// PortRequest FSM: requested/validated/completed/rejected.
pub mod port_request {
    const TRANSITIONS: &[(&str, &str, &str)] = &[
        ("validate", "requested", "validated"),
        ("complete", "requested", "completed"),
        ("complete", "validated", "completed"),
        ("reject", "requested", "rejected"),
        ("reject", "validated", "rejected"),
    ];
    pub fn is_valid_transition(from: &str, trigger: &str) -> bool {
        TRANSITIONS
            .iter()
            .any(|(t, s, _)| *t == trigger && *s == from)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn case_fsm() {
        assert!(case::is_valid_transition("open", "take"));
        assert_eq!(
            case::get_next_state("in_progress", "resolve"),
            Some("resolved")
        );
        assert_eq!(case::get_next_state("open", "resolve"), Some("resolved"));
        assert_eq!(case::get_next_state("open", "cancel"), Some("closed"));
        assert!(!case::is_valid_transition("closed", "take"));
        assert!(!case::is_valid_transition("resolved", "cancel"));
    }

    #[test]
    fn ticket_fsm() {
        assert_eq!(ticket::get_next_state("open", "ack"), Some("acknowledged"));
        assert_eq!(
            ticket::get_next_state("resolved", "reopen"),
            Some("in_progress")
        );
        assert_eq!(
            ticket::get_next_state("pending", "cancel"),
            Some("cancelled")
        );
        assert!(!ticket::is_valid_transition("resolved", "cancel"));
        assert!(ticket::CANCELLABLE.contains(&"in_progress"));
        assert!(ticket::TERMINAL.contains(&"closed"));
    }

    #[test]
    fn esim_fsm() {
        assert_eq!(
            esim::get_next_state("available", "reserve"),
            Some("reserved")
        );
        assert_eq!(
            esim::get_next_state("reserved", "release"),
            Some("available")
        );
        assert_eq!(
            esim::get_next_state("activated", "recycle"),
            Some("recycled")
        );
        assert!(!esim::is_valid_transition("reserved", "recycle"));
    }

    #[test]
    fn port_request_fsm() {
        assert!(port_request::is_valid_transition("requested", "complete"));
        assert!(port_request::is_valid_transition("validated", "reject"));
        assert!(!port_request::is_valid_transition("completed", "reject"));
    }
}
