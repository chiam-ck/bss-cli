//! SSE frame helpers shared across portals. Port of `bss_portal_ui.sse`.
//!
//! Both portals emit Server-Sent Event frames the same way: one `event:` line,
//! one `data:` line containing a single-line HTML partial, terminated by a blank
//! line. Centralising the encoding keeps escape rules uniform.

/// Encode one SSE frame. `html_line` MUST be a single line — embedded newlines
/// split the frame at the wrong boundary.
pub fn format_frame(event_name: &str, html_line: &str) -> Vec<u8> {
    format!("event: {event_name}\ndata: {html_line}\n\n").into_bytes()
}

/// Tiny fragment for the agent-log header status indicator.
pub fn status_html(status: &str) -> String {
    let cls = match status {
        "live" => "dot live",
        "done" => "dot done",
        "error" => "dot error",
        "idle" => "dot idle",
        _ => "dot idle",
    };
    format!("<span class=\"{cls}\"></span> {status}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_shape() {
        assert_eq!(
            format_frame("agent", "<p>hi</p>"),
            b"event: agent\ndata: <p>hi</p>\n\n".to_vec()
        );
    }

    #[test]
    fn status_classes() {
        assert_eq!(status_html("live"), "<span class=\"dot live\"></span> live");
        assert_eq!(status_html("done"), "<span class=\"dot done\"></span> done");
        assert_eq!(
            status_html("error"),
            "<span class=\"dot error\"></span> error"
        );
        assert_eq!(status_html("idle"), "<span class=\"dot idle\"></span> idle");
        // Unknown → idle class, echoes the raw status.
        assert_eq!(
            status_html("weird"),
            "<span class=\"dot idle\"></span> weird"
        );
    }
}
