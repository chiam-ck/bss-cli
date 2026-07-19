//! Small helpers for ASCII-box rendering shared by the hero renderers. Port of
//! `bss_cockpit.renderers._box`.
//!
//! **The load-bearing seam here is character-vs-byte width.** Python's `len()`,
//! slicing and `str.ljust()` all count *characters*; Rust's `str::len()` counts
//! *bytes*. Every line these helpers frame carries non-ASCII (`●`, `—`, `█`, the
//! box runes themselves), so a byte-based pad/truncate would both mis-align the
//! frame and risk slicing mid-codepoint. All width maths below is char-wise.

/// A coloured state indicator suitable for plain-text output.
///
/// No rich markup — renderer output is also fed to the LLM, so it must be
/// unambiguous ASCII-ish. The `●` plus the state name conveys state.
pub fn state_dot(state: &str) -> String {
    format!("● {}", state.to_uppercase())
}

/// The 8 partial-cell levels (index 0 is a space — "no partial").
const PARTIALS: [&str; 8] = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉"];

/// Render a `[████▌░░░]` progress bar with sub-character resolution.
///
/// The block-element gradient gives 8× sub-cell resolution, so 17%-vs-19% bars
/// look distinguishable instead of both rounding to `▏`. `total = None` means
/// unlimited (dash-filled).
pub fn progress_bar(used: f64, total: Option<f64>, width: usize) -> String {
    match total {
        None => format!("[{}]", "─".repeat(width)),
        // Python: `if total is None or total <= 0` — zero and negatives are the
        // dash-filled bar too, so the later `total == 0` branch is unreachable.
        Some(t) if t <= 0.0 => format!("[{}]", "─".repeat(width)),
        Some(t) => {
            let ratio = (used / t).clamp(0.0, 1.0);
            let scaled = ratio * width as f64;
            let full_cells = scaled as usize;
            let fractional = scaled - full_cells as f64;
            let partial_idx = (fractional * 8.0) as usize;
            let mut bar = "█".repeat(full_cells);
            if partial_idx > 0 && full_cells < width {
                bar.push_str(PARTIALS[partial_idx.min(7)]);
                bar.push_str(&"░".repeat(width - full_cells - 1));
            } else {
                bar.push_str(&"░".repeat(width.saturating_sub(full_cells)));
            }
            format!("[{bar}]")
        }
    }
}

/// The default `progress_bar` width (Python's `width: int = 26`).
pub const BAR_WIDTH: usize = 26;
/// The default `box` width (Python's `width: int = 62`).
pub const BOX_WIDTH: usize = 62;
/// The default `double_box` width (Python's `width: int = 64`).
pub const DOUBLE_BOX_WIDTH: usize = 64;

/// Truncate to `n` **characters** (Python `raw[:n]`), then pad to `n` characters
/// (Python `str.ljust(n)`).
fn fit(raw: &str, n: usize) -> String {
    let count = raw.chars().count();
    if count > n {
        raw.chars().take(n).collect()
    } else {
        let mut s = raw.to_string();
        s.push_str(&" ".repeat(n - count));
        s
    }
}

/// Wrap `lines` in a double-ruled (`╔ ═ ╗`) frame — visually heavier than [`box`].
/// Used to make `state=blocked` jump off the page.
pub fn double_box(lines: &[String], title: &str, width: usize) -> String {
    frame(lines, title, width, '═', "╔═ ", "╗", "╚", "╝", "║ ", " ║")
}

/// Wrap `lines` in a unicode ASCII box with `title` in the top border.
pub fn r#box(lines: &[String], title: &str, width: usize) -> String {
    frame(lines, title, width, '─', "┌─ ", "┐", "└", "┘", "│ ", " │")
}

#[allow(clippy::too_many_arguments)]
fn frame(
    lines: &[String],
    title: &str,
    width: usize,
    rule: char,
    top_open: &str,
    top_close: &str,
    bot_open: &str,
    bot_close: &str,
    row_open: &str,
    row_close: &str,
) -> String {
    let inner = width - 2;
    // Python: `"═" * max(0, inner - len(title) - 3)` — len() is char-wise.
    let fill = inner.saturating_sub(title.chars().count() + 3);
    let mut out = String::new();
    out.push_str(&format!(
        "{top_open}{title} {}{top_close}",
        rule.to_string().repeat(fill)
    ));
    for raw in lines {
        out.push('\n');
        out.push_str(row_open);
        out.push_str(&fit(raw, inner - 2));
        out.push_str(row_close);
    }
    out.push('\n');
    out.push_str(&format!(
        "{bot_open}{}{bot_close}",
        rule.to_string().repeat(inner)
    ));
    out
}

/// Format an 8-digit MSISDN as `XXXX XXXX`.
pub fn format_msisdn(msisdn: &str) -> String {
    if msisdn.chars().count() == 8 {
        let c: Vec<char> = msisdn.chars().collect();
        format!(
            "{} {}",
            c[..4].iter().collect::<String>(),
            c[4..].iter().collect::<String>()
        )
    } else {
        msisdn.to_string()
    }
}

/// Format an ICCID with spaces every 4 digits.
pub fn format_iccid(iccid: &str) -> String {
    iccid
        .chars()
        .collect::<Vec<_>>()
        .chunks(4)
        .map(|c| c.iter().collect::<String>())
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn state_dot_uppercases() {
        assert_eq!(state_dot("active"), "● ACTIVE");
        assert_eq!(state_dot("blocked"), "● BLOCKED");
        assert_eq!(state_dot(""), "● ");
    }

    /// Golden — captured from the Python oracle.
    #[test]
    fn progress_bar_matches_oracle() {
        // Unlimited (None) and non-positive totals → the dash bar.
        assert_eq!(
            progress_bar(0.0, None, BAR_WIDTH),
            "[──────────────────────────]"
        );
        assert_eq!(
            progress_bar(5.0, Some(0.0), BAR_WIDTH),
            "[──────────────────────────]"
        );
        // Empty / full.
        assert_eq!(
            progress_bar(0.0, Some(100.0), BAR_WIDTH),
            "[░░░░░░░░░░░░░░░░░░░░░░░░░░]"
        );
        assert_eq!(
            progress_bar(100.0, Some(100.0), BAR_WIDTH),
            "[██████████████████████████]"
        );
        // Half.
        assert_eq!(
            progress_bar(50.0, Some(100.0), BAR_WIDTH),
            "[█████████████░░░░░░░░░░░░░]"
        );
        // The sub-cell resolution the docstring promises: 17% vs 19% differ.
        assert_eq!(
            progress_bar(17.0, Some(100.0), BAR_WIDTH),
            "[████▍░░░░░░░░░░░░░░░░░░░░░]"
        );
        assert_eq!(
            progress_bar(19.0, Some(100.0), BAR_WIDTH),
            "[████▉░░░░░░░░░░░░░░░░░░░░░]"
        );
        // Over-full clamps rather than overflowing.
        assert_eq!(
            progress_bar(150.0, Some(100.0), BAR_WIDTH),
            "[██████████████████████████]"
        );
    }

    /// Every bar is exactly `width` cells wide regardless of ratio — the
    /// property a byte-based implementation would silently break.
    #[test]
    fn progress_bar_width_is_stable() {
        for used in 0..=100 {
            let bar = progress_bar(used as f64, Some(100.0), BAR_WIDTH);
            let inner: String = bar.chars().skip(1).take(BAR_WIDTH).collect();
            assert_eq!(bar.chars().count(), BAR_WIDTH + 2, "used={used} bar={bar}");
            assert_eq!(inner.chars().count(), BAR_WIDTH);
        }
    }

    #[test]
    fn box_matches_oracle() {
        let out = r#box(
            &["hello".to_string(), "world".to_string()],
            "Title",
            BOX_WIDTH,
        );
        let expected = "\
┌─ Title ────────────────────────────────────────────────────┐
│ hello                                                      │
│ world                                                      │
└────────────────────────────────────────────────────────────┘";
        assert_eq!(out, expected);
    }

    #[test]
    fn double_box_matches_oracle() {
        let out = double_box(&["blocked!".to_string()], "Sub", DOUBLE_BOX_WIDTH);
        let expected = "\
╔═ Sub ════════════════════════════════════════════════════════╗
║ blocked!                                                     ║
╚══════════════════════════════════════════════════════════════╝";
        assert_eq!(out, expected);
    }

    /// The char-vs-byte seam: a line of multi-byte runes must pad to the same
    /// visual width as an ASCII line, and every row must be equal length.
    #[test]
    fn box_pads_multibyte_lines_by_chars_not_bytes() {
        let out = r#box(
            &["● ACTIVE".to_string(), "12345678".to_string()],
            "T",
            BOX_WIDTH,
        );
        let widths: Vec<usize> = out.lines().map(|l| l.chars().count()).collect();
        assert!(
            widths.iter().all(|w| *w == BOX_WIDTH),
            "all rows are {BOX_WIDTH} chars wide, got {widths:?}"
        );
    }

    /// Over-long lines truncate at the character boundary (never mid-codepoint).
    #[test]
    fn box_truncates_long_lines_by_chars() {
        let long = "█".repeat(200);
        let out = r#box(&[long], "T", BOX_WIDTH);
        let body = out.lines().nth(1).unwrap();
        assert_eq!(body.chars().count(), BOX_WIDTH);
        assert_eq!(body.chars().filter(|c| *c == '█').count(), BOX_WIDTH - 4);
    }

    /// A title longer than the frame saturates the fill to zero rather than
    /// panicking on an underflow (Python's `max(0, ...)`).
    #[test]
    fn box_tolerates_an_over_long_title() {
        let out = r#box(&[], &"T".repeat(200), BOX_WIDTH);
        assert!(out.starts_with("┌─ TTT"));
    }

    #[test]
    fn format_msisdn_matches_oracle() {
        assert_eq!(format_msisdn("91234567"), "9123 4567");
        // Non-8-digit passes through untouched.
        assert_eq!(format_msisdn("+6591234567"), "+6591234567");
        assert_eq!(format_msisdn(""), "");
    }

    #[test]
    fn format_iccid_matches_oracle() {
        assert_eq!(
            format_iccid("8965000000000000001"),
            "8965 0000 0000 0000 001"
        );
        assert_eq!(format_iccid(""), "");
        assert_eq!(format_iccid("12"), "12");
    }
}
