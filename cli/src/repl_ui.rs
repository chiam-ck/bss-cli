//! Terminal presentation for the cockpit REPL — the ANSI-truecolor banner panel,
//! the green `bss ai` reply panels, and a light markdown→ANSI renderer. Port of the
//! Rich `Panel` / `Markdown` presentation in `cli/bss_cli/repl.py` (`_render_banner`,
//! `_LOGO`, `_SLASH_HELP`, and the reply `Panel(Markdown(...), border_style="green")`).
//!
//! Rich does the heavy lifting in Python (box-drawing, alignment, Markdown, ANSI
//! width). reedline gives us none of that, so this module reimplements the slice the
//! REPL actually uses: a rounded-box `panel()` drawer, run-based word wrapping that is
//! ANSI- and Unicode-width aware, and a small inline-markdown parser
//! (`**bold**`, `*italic*`, `` `code` ``, headings, bullets, fenced code). Colors
//! follow the operator branding's `theme.rich_accent` (a Rich color name), mapped to
//! an SGR foreground code — the same named-color palette Rich would have painted.

use bss_branding::BrandingView;
use bss_models::BSS_RELEASE;
use unicode_width::UnicodeWidthStr;

/// The ASCII wordmark — product art, byte-identical to Python's `_LOGO`. Stays the
/// same regardless of operator branding (the version footnote is the attribution).
const LOGO: &str = r" ██████╗  ███████╗ ███████╗     ██████╗ ██╗      ██╗
 ██╔══██╗ ██╔════╝ ██╔════╝    ██╔════╝ ██║      ██║
 ██████╔╝ ███████╗ ███████╗    ██║      ██║      ██║
 ██╔══██╗ ╚════██║ ╚════██║    ██║      ██║      ██║
 ██████╔╝ ███████║ ███████║    ╚██████╗ ███████╗ ██║
 ╚═════╝  ╚══════╝ ╚══════╝     ╚═════╝ ╚══════╝ ╚═╝";

// ─── ANSI helpers ─────────────────────────────────────────────────────

/// Wrap `text` in an SGR sequence (`codes` like `"1;32"`) with a trailing reset.
/// An empty `codes` returns the text unchanged (no escape noise for plain runs).
fn sgr(codes: &str, text: &str) -> String {
    if codes.is_empty() {
        text.to_string()
    } else {
        format!("\x1b[{codes}m{text}\x1b[0m")
    }
}

/// A Rich color *name* → SGR foreground code. Covers the palette the themes use
/// (`rich_accent` is one of these) plus the fixed names the banner/reply chrome
/// references. Unknown names fall back to default foreground (`"39"`).
fn fg(name: &str) -> &'static str {
    match name {
        "green" => "32",
        "yellow" => "33",
        "cyan" => "36",
        "magenta" => "35",
        "blue" => "34",
        "red" => "31",
        "white" => "37",
        "dim" => "2",
        _ => "39",
    }
}

/// Strip CSI SGR sequences (`\x1b[…m`) so width math counts only visible glyphs.
fn strip_ansi(s: &str) -> String {
    let mut out = String::new();
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            // Consume up to and including the terminating letter (we only emit `…m`).
            for n in chars.by_ref() {
                if n.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// Display width of a possibly-ANSI-styled string (East-Asian aware, escapes elided).
fn vis_width(s: &str) -> usize {
    UnicodeWidthStr::width(strip_ansi(s).as_str())
}

fn dashes(n: usize) -> String {
    "─".repeat(n)
}

// ─── Panel drawer ─────────────────────────────────────────────────────

/// Horizontal alignment of a body line within the panel's content column.
#[derive(Clone, Copy)]
enum Align {
    Left,
    Center,
}

/// One rendered body row (may already carry ANSI); its visible width is measured,
/// never assumed from `.len()`.
struct Row {
    text: String,
    align: Align,
}

impl Row {
    fn left(text: impl Into<String>) -> Self {
        Row {
            text: text.into(),
            align: Align::Left,
        }
    }
    fn center(text: impl Into<String>) -> Self {
        Row {
            text: text.into(),
            align: Align::Center,
        }
    }
    fn blank() -> Self {
        Row::left("")
    }
}

/// Draw a Rich-style rounded panel: the `title` sits in the top border (left-aligned),
/// the `subtitle` in the bottom border (right-aligned), one space of horizontal
/// padding inside each border. `border` is a Rich color name; the box chrome is
/// painted in it, body/title text keep whatever ANSI they already carry.
fn panel(
    title: Option<&str>,
    subtitle: Option<&str>,
    rows: &[Row],
    border: &str,
    width: usize,
) -> String {
    let code = fg(border);
    let bc = |s: &str| sgr(code, s);
    let interior = width.saturating_sub(2); // between the two corners
    let content_w = width.saturating_sub(4); // borders + 1-space padding each side

    let mut out = String::new();

    // Top border: ╭─ {title} ─────╮
    out.push_str(&bc("╭"));
    match title {
        Some(t) => {
            let used = 2 + vis_width(t) + 1; // "─ " + title + " "
            out.push_str(&bc("─ "));
            out.push_str(t);
            out.push(' ');
            out.push_str(&bc(&dashes(interior.saturating_sub(used))));
        }
        None => out.push_str(&bc(&dashes(interior))),
    }
    out.push_str(&bc("╮"));
    out.push('\n');

    // Body rows.
    for row in rows {
        let vis = vis_width(&row.text);
        let slack = content_w.saturating_sub(vis);
        let (lpad, rpad) = match row.align {
            Align::Left => (0, slack),
            Align::Center => (slack / 2, slack - slack / 2),
        };
        out.push_str(&bc("│"));
        out.push(' ');
        out.push_str(&" ".repeat(lpad));
        out.push_str(&row.text);
        out.push_str(&" ".repeat(rpad));
        out.push(' ');
        out.push_str(&bc("│"));
        out.push('\n');
    }

    // Bottom border: ╰──────── {subtitle} ─╯
    out.push_str(&bc("╰"));
    match subtitle {
        Some(s) => {
            let used = 1 + vis_width(s) + 1 + 1; // dashes + " " + sub + " " + trailing "─"
            out.push_str(&bc(&dashes(interior.saturating_sub(used))));
            out.push(' ');
            out.push_str(s);
            out.push(' ');
            out.push_str(&bc("─"));
        }
        None => out.push_str(&bc(&dashes(interior))),
    }
    out.push_str(&bc("╯"));
    out
}

// ─── Styled-run word wrapping ─────────────────────────────────────────

/// A run of text sharing one SGR style (`sgr` codes, empty = plain).
struct Tok {
    text: String,
    sgr: String,
}

impl Tok {
    fn new(text: impl Into<String>, sgr: &str) -> Self {
        Tok {
            text: text.into(),
            sgr: sgr.to_string(),
        }
    }
}

/// Word-wrap styled tokens to `width` visible columns, collapsing runs of spaces to
/// one (prose/hints — code blocks bypass this). Each returned line carries ANSI and
/// measures `<= width`.
fn wrap_toks(toks: &[Tok], width: usize) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut cur_w = 0usize;
    for tok in toks {
        for word in tok.text.split(' ') {
            if word.is_empty() {
                continue;
            }
            let ww = UnicodeWidthStr::width(word);
            let sep = usize::from(cur_w > 0);
            if cur_w > 0 && cur_w + sep + ww > width {
                lines.push(std::mem::take(&mut cur));
                cur_w = 0;
            }
            if cur_w > 0 {
                cur.push(' ');
                cur_w += 1;
            }
            cur.push_str(&sgr(&tok.sgr, word));
            cur_w += ww;
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

// ─── Light markdown → styled tokens ───────────────────────────────────

/// Inline SGR for the current emphasis flags (code overrides — monospace/dim).
fn inline_sgr(bold: bool, ital: bool) -> String {
    let mut parts: Vec<&str> = Vec::new();
    if bold {
        parts.push("1");
    }
    if ital {
        parts.push("3");
    }
    parts.join(";")
}

/// Parse one line of inline markdown into styled runs: `**bold**`, `*italic*` /
/// `_italic_`, and `` `code` ``. Unbalanced markers just stay literal-ish — this is a
/// display nicety, not a spec-complete parser.
fn parse_inline(s: &str) -> Vec<Tok> {
    let mut toks: Vec<Tok> = Vec::new();
    let mut buf = String::new();
    let (mut bold, mut ital, mut code) = (false, false, false);
    let chars: Vec<char> = s.chars().collect();
    let mut i = 0;
    let flush = |toks: &mut Vec<Tok>, buf: &mut String, style: &str| {
        if !buf.is_empty() {
            toks.push(Tok::new(std::mem::take(buf), style));
        }
    };
    while i < chars.len() {
        let c = chars[i];
        if code {
            if c == '`' {
                flush(&mut toks, &mut buf, "2;36");
                code = false;
                i += 1;
                continue;
            }
            buf.push(c);
            i += 1;
            continue;
        }
        if c == '`' {
            flush(&mut toks, &mut buf, &inline_sgr(bold, ital));
            code = true;
            i += 1;
            continue;
        }
        if c == '*' && i + 1 < chars.len() && chars[i + 1] == '*' {
            flush(&mut toks, &mut buf, &inline_sgr(bold, ital));
            bold = !bold;
            i += 2;
            continue;
        }
        if c == '_' {
            // CommonMark treats intraword underscores as literal, so domain ids like
            // `PLAN_S` / `PLAN_L` don't turn into emphasis. Only toggle at a word edge.
            let prev_alnum = i > 0 && chars[i - 1].is_alphanumeric();
            let next_alnum = i + 1 < chars.len() && chars[i + 1].is_alphanumeric();
            if prev_alnum && next_alnum {
                buf.push(c);
                i += 1;
                continue;
            }
            flush(&mut toks, &mut buf, &inline_sgr(bold, ital));
            ital = !ital;
            i += 1;
            continue;
        }
        if c == '*' {
            flush(&mut toks, &mut buf, &inline_sgr(bold, ital));
            ital = !ital;
            i += 1;
            continue;
        }
        buf.push(c);
        i += 1;
    }
    let style = if code {
        "2;36".to_string()
    } else {
        inline_sgr(bold, ital)
    };
    flush(&mut toks, &mut buf, &style);
    toks
}

/// Render markdown `text` to panel body rows wrapped to `content_w`. Handles ATX
/// headings (`#…` → bold), bullets (`- `/`* ` → `• `), and fenced code blocks
/// (```` ``` ```` → dim cyan, verbatim, no inline parsing). Everything else is inline
/// markdown, word-wrapped. Mirrors Rich `Markdown(...)` closely enough for the reply
/// panel; a plain-text reply passes through unchanged.
fn markdown_rows(text: &str, content_w: usize) -> Vec<Row> {
    let mut rows: Vec<Row> = Vec::new();
    let mut in_code = false;
    for line in text.trim_end().split('\n') {
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") {
            in_code = !in_code;
            continue;
        }
        if in_code {
            rows.push(Row::left(sgr("2;36", line)));
            continue;
        }
        if trimmed.is_empty() {
            rows.push(Row::blank());
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix('#') {
            let heading = rest.trim_start_matches('#').trim();
            for wrapped in wrap_toks(&[Tok::new(heading, "1")], content_w) {
                rows.push(Row::left(wrapped));
            }
            continue;
        }
        if let Some(rest) = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
        {
            let mut toks = vec![Tok::new("• ", fg("green"))];
            toks.extend(parse_inline(rest));
            for wrapped in wrap_toks(&toks, content_w) {
                rows.push(Row::left(wrapped));
            }
            continue;
        }
        let toks = parse_inline(trimmed);
        for wrapped in wrap_toks(&toks, content_w) {
            rows.push(Row::left(wrapped));
        }
    }
    rows
}

// ─── Public surface ───────────────────────────────────────────────────

/// Terminal width, clamped to a readable band (Rich auto-detects; reedline doesn't).
fn banner_width() -> usize {
    crossterm::terminal::size()
        .map(|(cols, _)| cols as usize)
        .unwrap_or(80)
        .clamp(60, 100)
}

/// The colored slash-command hint line (port of `_SLASH_HELP`) as styled tokens —
/// commands in green, labels plain — so it word-wraps cleanly inside the banner.
fn slash_help_toks() -> Vec<Tok> {
    let g = fg("green");
    // (text, is_command) — commands green, labels plain. Mirrors `_SLASH_HELP`.
    let spec: &[(&str, bool)] = &[
        ("/sessions", true),
        ("list", false),
        ("/new", true),
        ("[label]", false),
        ("/switch", true),
        ("SES", false),
        ("/reset", true),
        ("/focus", true),
        ("CUST", false),
        ("/360", true),
        ("/ports", true),
        ("/confirm", true),
        ("/config edit", true),
        ("/operator edit", true),
        ("/help", true),
        ("/exit", true),
    ];
    spec.iter()
        .map(|(text, is_cmd)| Tok::new(*text, if *is_cmd { g } else { "" }))
        .collect()
}

/// Render the cockpit start/switch banner. Port of `_render_banner`: branded rounded
/// panel with the ASCII logo (bold accent), tagline, actor/model + session/focus meta,
/// the "try" examples, the slash-command hints, and the dim `bss-cli vX.Y.Z` footnote.
/// A destructive-default run adds the red warning strip.
pub fn banner(
    brand: &BrandingView,
    actor: &str,
    model: &str,
    session_id: &str,
    focus: &str,
    allow_destructive_default: bool,
) -> String {
    let width = banner_width();
    let content_w = width.saturating_sub(4);
    let accent = brand.theme.rich_accent;
    let logo_style = format!("1;{}", fg(accent));

    let mut rows: Vec<Row> = Vec::new();
    for line in LOGO.split('\n') {
        rows.push(Row::center(sgr(&logo_style, line)));
    }
    rows.push(Row::blank());
    rows.push(Row::center(format!(
        "{}   {}   {}   {}   {}",
        sgr("1;37", &brand.brand_name),
        sgr("2", "·"),
        sgr("1;37", "LLM-native Business Support System"),
        sgr("2", "·"),
        sgr(fg("magenta"), "operator cockpit"),
    )));
    rows.push(Row::center(format!(
        "{} {}   {}   {} {}",
        sgr("2", "actor"),
        sgr(fg("green"), actor),
        sgr("2", "·"),
        sgr("2", "model"),
        sgr("1;35", model),
    )));
    rows.push(Row::center(format!(
        "{} {}   {}   {} {}",
        sgr("2", "session"),
        sgr(fg("yellow"), session_id),
        sgr("2", "·"),
        sgr("2", "focus"),
        sgr(fg("green"), focus),
    )));
    rows.push(Row::blank());

    // "try" examples — styled tokens, wrapped.
    let try_toks = vec![
        Tok::new("try", "1"),
        Tok::new("show the catalog", "3;32"),
        Tok::new("·", "2"),
        Tok::new("show subscription SUB-0001", "3;32"),
        Tok::new("·", "2"),
        Tok::new("/360 CUST-001", "3;32"),
    ];
    for line in wrap_toks(&try_toks, content_w) {
        rows.push(Row::left(line));
    }
    // "slash" hint line — bold label + green commands, wrapped.
    let mut slash_toks = vec![Tok::new("slash", "1")];
    slash_toks.extend(slash_help_toks());
    for line in wrap_toks(&slash_toks, content_w) {
        rows.push(Row::left(line));
    }

    rows.push(Row::center(sgr("2", &format!("bss-cli v{BSS_RELEASE}"))));

    if allow_destructive_default {
        rows.push(Row::blank());
        rows.push(Row::center(format!(
            "{} {}",
            sgr("1;31;43", " DESTRUCTIVE-DEFAULT MODE "),
            sgr(fg("red"), "writes execute without /confirm — beware"),
        )));
    }

    let title = format!(
        "{} {} {}",
        sgr(&format!("1;{}", fg(accent)), &brand.mark),
        sgr("37", &brand.brand_name),
        sgr("2", "· cockpit"),
    );
    let subtitle = sgr("2", "type a request or a /command");
    panel(Some(&title), Some(&subtitle), &rows, accent, width)
}

/// Render the agent's prose reply as a green `bss ai` panel with markdown formatting.
/// Port of `Panel(Markdown(final_text), title="bss ai", border_style="green")`.
pub fn reply_panel(text: &str) -> String {
    let width = banner_width();
    let content_w = width.saturating_sub(4);
    let body = if text.trim().is_empty() {
        vec![Row::left(sgr("2", "(no reply)"))]
    } else {
        markdown_rows(text, content_w)
    };
    panel(Some(&sgr("1", "bss ai")), None, &body, "green", width)
}

/// Paint `text` with a Rich color name (`"green"`, `"yellow"`, `"dim"`, …). Public so
/// slash-command handlers can color aligned table columns without touching SGR codes.
pub fn paint(color: &str, text: &str) -> String {
    sgr(fg(color), text)
}

/// Truncate a possibly-ANSI-styled string to `max` visible columns, closing any open
/// SGR run with a reset so a cut never bleeds color past the panel border.
fn truncate_visible(s: &str, max: usize) -> String {
    use unicode_width::UnicodeWidthChar;
    let mut out = String::new();
    let mut w = 0usize;
    let mut had_escape = false;
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            out.push(c);
            for n in chars.by_ref() {
                out.push(n);
                if n.is_ascii_alphabetic() {
                    break;
                }
            }
            had_escape = true;
            continue;
        }
        let cw = UnicodeWidthChar::width(c).unwrap_or(0);
        if w + cw > max {
            if had_escape {
                out.push_str("\x1b[0m");
            }
            return out;
        }
        out.push(c);
        w += cw;
    }
    out
}

/// Draw a titled panel around already-formatted `lines` (verbatim, left-aligned) with
/// the named `border` color. Each line is truncated to the content width so the box
/// stays intact regardless of terminal size. Used by the session slash commands
/// (`/sessions` table, `/switch` prior-turns preview).
pub fn framed(title: &str, lines: Vec<String>, border: &str) -> String {
    let width = banner_width();
    let content_w = width.saturating_sub(4);
    let rows: Vec<Row> = lines
        .into_iter()
        .map(|l| Row::left(truncate_visible(&l, content_w)))
        .collect();
    panel(Some(&sgr("1", title)), None, &rows, border, width)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn strip_ansi_removes_sgr() {
        assert_eq!(strip_ansi("\x1b[1;32mhi\x1b[0m"), "hi");
        assert_eq!(vis_width("\x1b[33mSES-01\x1b[0m"), 6);
    }

    #[test]
    fn wrap_respects_visible_width() {
        let toks = vec![Tok::new("aaaa bbbb cccc", "")];
        let lines = wrap_toks(&toks, 9);
        // "aaaa bbbb" = 9 fits; "cccc" wraps.
        assert_eq!(lines.len(), 2);
        assert!(vis_width(&lines[0]) <= 9);
    }

    #[test]
    fn inline_parses_bold_and_code() {
        let toks = parse_inline("a **b** `c`");
        let joined: String = toks.iter().map(|t| t.text.as_str()).collect();
        assert_eq!(joined, "a b c");
        // The bold run carries SGR 1, the code run 2;36.
        assert!(toks.iter().any(|t| t.text == "b" && t.sgr == "1"));
        assert!(toks.iter().any(|t| t.text == "c" && t.sgr == "2;36"));
    }

    #[test]
    fn intraword_underscore_stays_literal() {
        // Domain ids like PLAN_S must not become italic (CommonMark intraword rule).
        let toks = parse_inline("PLAN_S — 5GB");
        let joined: String = toks.iter().map(|t| t.text.as_str()).collect();
        assert_eq!(joined, "PLAN_S — 5GB");
        assert!(toks.iter().all(|t| !t.sgr.contains('3')));
        // A boundary underscore still emphasises.
        let em = parse_inline("_hi_");
        assert!(em.iter().any(|t| t.text == "hi" && t.sgr == "3"));
    }

    #[test]
    fn panel_borders_and_rows_present() {
        let out = panel(Some("T"), Some("S"), &[Row::left("hi")], "green", 30);
        let lines: Vec<&str> = out.split('\n').collect();
        assert!(lines.first().unwrap().contains('╭'));
        assert!(lines.last().unwrap().contains('╯'));
        // Every rendered line strips to exactly the panel width.
        for l in &lines {
            assert_eq!(vis_width(l), 30, "line {l:?}");
        }
    }

    #[test]
    fn reply_panel_wraps_plain_text() {
        let out = reply_panel("hello world");
        assert!(out.contains("bss ai"));
        assert!(out.contains("hello"));
    }
}
