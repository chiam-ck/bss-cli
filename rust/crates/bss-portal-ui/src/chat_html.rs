//! Chat-bubble HTML renderers shared by the customer-chat surface (v0.12) and
//! the operator-cockpit chat thread (v0.13). Port of `bss_portal_ui.chat_html`.
//!
//! Doctrine: the LLM's output is hostile-by-default. We HTML-escape every
//! character first, then convert a whitelisted set of markdown tokens into HTML.
//! No raw-HTML pass-through; no link/image rendering. Same shape + same security
//! boundary on both surfaces so they cannot drift apart.
//!
//! Channel-markup + reasoning-leakage stripping is delegated to
//! `bss_cockpit::postprocess` (single source of truth). Lookaround italics need
//! `fancy-regex` (the `regex` crate can't do `(?<!\*)`/`(?!\*)`).

use std::sync::LazyLock;

use fancy_regex::{Captures, Regex};

use bss_cockpit::postprocess::{strip_channel_markup, strip_reasoning_leakage as strip_reasoning};

#[allow(clippy::expect_used)]
fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static chat_html regex compiles")
}

// Inline patterns.
static RE_BOLD: LazyLock<Regex> = LazyLock::new(|| re(r"\*\*(?P<inner>[^*\n]+)\*\*"));
static RE_ITALIC_AST: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?<!\*)\*(?P<inner>[^*\n]+)\*(?!\*)"));
static RE_ITALIC_UND: LazyLock<Regex> = LazyLock::new(|| re(r"(?<!\w)_(?P<inner>[^_\n]+)_(?!\w)"));
static RE_CODE: LazyLock<Regex> = LazyLock::new(|| re(r"`(?P<inner>[^`\n]+)`"));

// Block-level patterns (operate on already-HTML-escaped text).
static RE_LIST_ITEM: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*[\*\-]\s+(?P<body>.*)$"));
static RE_OL_ITEM: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*\d+[.)]\s+(?P<body>.*)$"));
static RE_HEADING: LazyLock<Regex> =
    LazyLock::new(|| re(r"^(?P<hashes>#{1,4})\s+(?P<body>.+?)\s*#*\s*$"));
static RE_CODE_FENCE: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*```"));
static RE_TABLE_ROW: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*\|.+\|\s*$"));
static RE_TABLE_SEP_CELL: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*:?-{2,}:?\s*$"));
// Rich/box-drawing ASCII panel detection.
const BOX_CHARS: &str = "─━│┃┌┐└┘├┤┬┴┼═║╔╗╚╝";
static RE_ASCII_PANEL_LINE: LazyLock<Regex> = LazyLock::new(|| re(&format!("[{BOX_CHARS}]")));

/// `re.match(line)` — a match anchored at the start (fancy-regex `captures`;
/// the patterns already carry `^`). A backtracking error is treated as no match.
fn matches(re: &Regex, line: &str) -> bool {
    re.is_match(line).unwrap_or(false)
}

/// Python `html.escape(s, quote=True)`: `&`→`&amp;` first, then `<`/`>`/`"`/`'`.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

/// Combined strip used before rendering (chat_html's internal
/// `_strip_reasoning_leakage`): channel markup first, then reasoning leakage.
fn strip_leakage(text: &str) -> String {
    strip_reasoning(&strip_channel_markup(text))
}

/// Public alias for callers that sanitize text before persisting it. Matches
/// `chat_html.strip_reasoning_leakage` (the combined strip).
pub fn strip_reasoning_leakage(text: &str) -> String {
    strip_leakage(text)
}

/// Replace every match, substituting `<tag>inner</tag>` for capture group 1.
fn wrap_all(re: &Regex, input: &str, open: &str, close: &str) -> String {
    re.replace_all(input, |caps: &Captures| {
        let inner = caps.get(1).map(|m| m.as_str()).unwrap_or("");
        format!("{open}{inner}{close}")
    })
    .into_owned()
}

/// Apply inline markdown to a single, already-escaped line. Order: code spans
/// first (they shadow `*`), bold before italic.
fn render_inline(line: &str) -> String {
    let out = wrap_all(&RE_CODE, line, "<code>", "</code>");
    let out = wrap_all(&RE_BOLD, &out, "<strong>", "</strong>");
    let out = wrap_all(&RE_ITALIC_AST, &out, "<em>", "</em>");
    wrap_all(&RE_ITALIC_UND, &out, "<em>", "</em>")
}

/// Parse a `| a | b | c |` row into `["a", "b", "c"]`.
fn split_table_row(row: &str) -> Vec<String> {
    let mut trimmed = row.trim();
    trimmed = trimmed.strip_prefix('|').unwrap_or(trimmed);
    trimmed = trimmed.strip_suffix('|').unwrap_or(trimmed);
    trimmed.split('|').map(|c| c.trim().to_string()).collect()
}

fn is_table_separator(row: &str) -> bool {
    let cells = split_table_row(row);
    if cells.is_empty() {
        return false;
    }
    cells.iter().all(|c| matches(&RE_TABLE_SEP_CELL, c))
}

#[derive(Default)]
struct Blocks {
    out: Vec<String>,
    para: Vec<String>,
    ul: Vec<String>,
    ol: Vec<String>,
}

impl Blocks {
    fn flush_ul(&mut self) {
        if !self.ul.is_empty() {
            let items: String = self
                .ul
                .iter()
                .map(|it| format!("<li>{}</li>", render_inline(it)))
                .collect();
            self.out.push(format!("<ul>{items}</ul>"));
            self.ul.clear();
        }
    }
    fn flush_ol(&mut self) {
        if !self.ol.is_empty() {
            let items: String = self
                .ol
                .iter()
                .map(|it| format!("<li>{}</li>", render_inline(it)))
                .collect();
            self.out.push(format!("<ol>{items}</ol>"));
            self.ol.clear();
        }
    }
    fn flush_para(&mut self) {
        if !self.para.is_empty() {
            let joined = self
                .para
                .iter()
                .map(|p| render_inline(p))
                .collect::<Vec<_>>()
                .join("<br>");
            self.out.push(format!("<p>{joined}</p>"));
            self.para.clear();
        }
    }
    fn flush_blocks(&mut self) {
        self.flush_ul();
        self.flush_ol();
        self.flush_para();
    }
}

/// Block-level + inline markdown render for assistant chat output. `allow_tables`
/// (v0.20.1) opts into pipe-table → `<table>` (default off preserves the v0.19
/// doctrine that pipe tables in prose are usually a hallucination).
pub fn render_chat_markdown(text: &str, allow_tables: bool) -> String {
    let cleaned = strip_leakage(text);
    let escaped = html_escape(&cleaned);
    let lines: Vec<&str> = escaped.split('\n').collect();

    let mut b = Blocks::default();
    let mut fence_buf: Option<Vec<String>> = None;

    let mut i = 0;
    while i < lines.len() {
        let line = lines[i].trim_end().to_string();

        // Code fence: collect until closing fence.
        if let Some(buf) = fence_buf.as_mut() {
            if matches(&RE_CODE_FENCE, &line) {
                b.out
                    .push(format!("<pre><code>{}</code></pre>", buf.join("\n")));
                fence_buf = None;
            } else {
                buf.push(line);
            }
            i += 1;
            continue;
        }
        if matches(&RE_CODE_FENCE, &line) {
            b.flush_blocks();
            fence_buf = Some(Vec::new());
            i += 1;
            continue;
        }

        // ASCII panel (Rich/box-drawing) → one <pre> block.
        if matches(&RE_ASCII_PANEL_LINE, &line) {
            b.flush_blocks();
            let mut panel = vec![line];
            let mut j = i + 1;
            while j < lines.len() && matches(&RE_ASCII_PANEL_LINE, lines[j]) {
                panel.push(lines[j].trim_end().to_string());
                j += 1;
            }
            b.out
                .push(format!("<pre><code>{}</code></pre>", panel.join("\n")));
            i = j;
            continue;
        }

        // Pipe-table grammar (opt-in via allow_tables). Kept as nested `if`s to
        // mirror the Python: a table row whose next line ISN'T a separator falls
        // THROUGH to paragraph handling (literal pipes survive), it doesn't
        // `continue` here.
        #[allow(clippy::collapsible_if)]
        if allow_tables && matches(&RE_TABLE_ROW, &line) {
            if i + 1 < lines.len() && is_table_separator(lines[i + 1]) {
                b.flush_blocks();
                let header_cells = split_table_row(&line);
                let mut body_rows: Vec<Vec<String>> = Vec::new();
                let mut j = i + 2;
                while j < lines.len() && matches(&RE_TABLE_ROW, lines[j]) {
                    body_rows.push(split_table_row(lines[j]));
                    j += 1;
                }
                let thead: String = format!(
                    "<thead><tr>{}</tr></thead>",
                    header_cells
                        .iter()
                        .map(|c| format!("<th>{}</th>", render_inline(c)))
                        .collect::<String>()
                );
                let tbody: String = format!(
                    "<tbody>{}</tbody>",
                    body_rows
                        .iter()
                        .map(|row| format!(
                            "<tr>{}</tr>",
                            row.iter()
                                .map(|c| format!("<td>{}</td>", render_inline(c)))
                                .collect::<String>()
                        ))
                        .collect::<String>()
                );
                b.out.push(format!("<table>{thead}{tbody}</table>"));
                i = j;
                continue;
            }
        }

        // Heading.
        if let Ok(Some(caps)) = RE_HEADING.captures(&line) {
            b.flush_blocks();
            let depth = caps.name("hashes").map(|m| m.as_str().len()).unwrap_or(1);
            let tag = format!("h{}", (2 + depth).clamp(3, 6));
            let body = caps.name("body").map(|m| m.as_str()).unwrap_or("");
            b.out
                .push(format!("<{tag}>{}</{tag}>", render_inline(body)));
            i += 1;
            continue;
        }

        // Unordered list.
        if let Ok(Some(caps)) = RE_LIST_ITEM.captures(&line) {
            b.flush_para();
            b.flush_ol();
            b.ul.push(
                caps.name("body")
                    .map(|m| m.as_str())
                    .unwrap_or("")
                    .to_string(),
            );
            i += 1;
            continue;
        }

        // Ordered list.
        if let Ok(Some(caps)) = RE_OL_ITEM.captures(&line) {
            b.flush_para();
            b.flush_ul();
            b.ol.push(
                caps.name("body")
                    .map(|m| m.as_str())
                    .unwrap_or("")
                    .to_string(),
            );
            i += 1;
            continue;
        }

        // Blank line — paragraph / list break.
        if line.trim().is_empty() {
            b.flush_blocks();
            i += 1;
            continue;
        }

        // Plain paragraph line.
        b.flush_ul();
        b.flush_ol();
        b.para.push(line);
        i += 1;
    }

    if let Some(buf) = fence_buf {
        b.out
            .push(format!("<pre><code>{}</code></pre>", buf.join("\n")));
    }
    b.flush_blocks();

    let joined: String = b.out.join("");
    if joined.is_empty() {
        "&nbsp;".to_string()
    } else {
        joined
    }
}

/// Full assistant reply as a chat bubble. Single-line HTML for SSE (embedded
/// newlines from tables/fences are stripped — HTML ignores them there).
pub fn render_assistant_bubble(text: &str, error: bool, allow_tables: bool) -> String {
    let mut css = "chat-bubble chat-bubble-assistant".to_string();
    if error {
        css.push_str(" chat-bubble-error");
    }
    let rendered = render_chat_markdown(text, allow_tables).replace('\n', "");
    format!("<div class=\"{css}\">{rendered}</div>")
}

/// Inline pill announcing a tool call. Tool name is HTML-escaped (untrusted).
pub fn render_tool_pill(tool_name: &str) -> String {
    format!(
        "<div class=\"chat-tool-pill\"><span class=\"chat-tool-icon\">≈</span>\
         <span class=\"chat-tool-name\">{}</span></div>",
        html_escape(tool_name)
    )
}
