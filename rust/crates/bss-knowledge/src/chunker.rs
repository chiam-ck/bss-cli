//! Markdown → chunks. One chunk per `##` or `###` heading section. Port of
//! `packages/bss-knowledge/bss_knowledge/chunker.py`.
//!
//! Split policy is per-file (see [`heading_chunk_levels`]):
//! * handbook + ARCHITECTURE nest deep → split on `##` AND `###`.
//! * DECISIONS.md → dated `## YYYY-MM-DD` entries, split on `##` only.
//! * everything else → `##` only.
//!
//! The anchor algorithm matches GitHub's: lowercase, spaces → hyphens, strip
//! non-word chars. The heading-path trail and the exact heading-stack update
//! order (stack updated *before* the prior chunk is flushed) are reproduced
//! verbatim — behaviour-frozen, quirks included.

use std::collections::BTreeMap;

use regex::Regex;

/// A chunked markdown section.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chunk {
    pub source_path: String,
    pub anchor: String,
    pub heading_path: String,
    pub content: String,
}

// The four regexes are compile-time-constant literals; a failure to compile is
// a programmer error caught by the first test run, so `expect` is the right
// tool here (same disposition as bss-middleware's static-salt build).
#[allow(clippy::expect_used)]
fn anchor_strip_re() -> &'static Regex {
    // `[^\w\- ]+` with Unicode `\w` (regex crate is Unicode-aware by default),
    // matching Python's `re.UNICODE`.
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"[^\w\- ]+").expect("static anchor-strip regex"))
}

#[allow(clippy::expect_used)]
fn ws_run_re() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\s+").expect("static whitespace-run regex"))
}

#[allow(clippy::expect_used)]
fn frontmatter_re() -> &'static Regex {
    // `\A---\s*\n(.*?\n)?---\s*\n` with DOTALL. Anchored at start, so only the
    // leading frontmatter block matches.
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?s)\A---\s*\n(.*?\n)?---\s*\n").expect("static frontmatter regex")
    })
}

#[allow(clippy::expect_used)]
fn heading_re() -> &'static Regex {
    // `^(#{1,6})\s+(.+?)\s*$`. Matched per line (line still carries its
    // trailing `\n`); with the regex crate's default `$` = end-of-haystack the
    // trailing `\s*` consumes the newline, reproducing Python's `.match`.
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(#{1,6})\s+(.+?)\s*$").expect("static heading regex"))
}

/// GitHub-flavoured anchor: lowercase, drop non-word chars, whitespace runs →
/// single hyphen, strip leading/trailing hyphens.
fn to_anchor(heading_text: &str) -> String {
    let s = heading_text.trim().to_lowercase();
    let s = anchor_strip_re().replace_all(&s, "");
    let s = ws_run_re().replace_all(&s, "-");
    s.trim_matches('-').to_string()
}

fn strip_frontmatter(text: &str) -> String {
    frontmatter_re().replace(text, "").into_owned()
}

/// Per-file split policy: which heading levels start a new chunk?
fn heading_chunk_levels(source_path: &str) -> &'static [usize] {
    match source_path {
        // Handbook + architecture nest deep; split on ## AND ###.
        "docs/HANDBOOK.md" | "ARCHITECTURE.md" => &[2, 3],
        // Dated entries are `## YYYY-MM-DD`. Split there only.
        "DECISIONS.md" => &[2],
        // Default: ## only.
        _ => &[2],
    }
}

/// Split lines keeping the `\n` terminator (mirrors Python
/// `str.splitlines(keepends=True)` for the `\n`-terminated doc corpus).
fn splitlines_keepends(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let bytes = text.as_bytes();
    let mut start = 0usize;
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'\n' {
            out.push(&text[start..=i]);
            start = i + 1;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// Split a markdown doc into chunks. Returns at least one chunk (the preamble)
/// when there are no matching headings.
pub fn chunk_markdown(source_path: &str, text: &str) -> Vec<Chunk> {
    let text = strip_frontmatter(text);
    let levels = heading_chunk_levels(source_path);

    let lines = splitlines_keepends(&text);
    let mut chunks: Vec<Chunk> = Vec::new();
    let mut current_lines: Vec<&str> = Vec::new();
    // heading level → text; deeper levels dropped when a shallower heading hits.
    let mut heading_stack: BTreeMap<usize, String> = BTreeMap::new();
    let mut current_heading: Option<String> = None;
    let mut current_level: Option<usize> = None;

    // flush the accumulated `current_lines` as a chunk, using the *current*
    // heading/level and the heading_stack as it stands right now (Python reads
    // the stack after it has already recorded the incoming heading — the exact
    // ordering quirk is preserved).
    let flush = |chunks: &mut Vec<Chunk>,
                 current_lines: &[&str],
                 current_heading: &Option<String>,
                 current_level: Option<usize>,
                 heading_stack: &BTreeMap<usize, String>| {
        let joined: String = current_lines.concat();
        let body = joined.trim_end();
        if body.is_empty() {
            return;
        }
        let (anchor, heading_path) = match current_heading {
            None => {
                // Preamble before any heading. Anchor + path are best-effort.
                let anchor = to_anchor(&source_path.replace('/', "-"));
                (anchor, source_path.to_string())
            }
            Some(heading) => {
                let anchor = to_anchor(heading);
                let lvl = current_level.unwrap_or(0);
                let mut trail: Vec<String> = heading_stack
                    .iter()
                    .filter(|(k, _)| **k < lvl)
                    .map(|(_, v)| v.clone())
                    .collect();
                trail.push(heading.clone());
                (anchor, trail.join(" \u{2192} "))
            }
        };
        chunks.push(Chunk {
            source_path: source_path.to_string(),
            anchor,
            heading_path,
            content: body.to_string(),
        });
    };

    for line in lines {
        if let Some(m) = heading_re().captures(line) {
            let level = m.get(1).map_or(0, |g| g.as_str().len());
            let heading_text = m.get(2).map_or("", |g| g.as_str()).to_string();
            // Update heading stack: drop deeper-or-equal levels, set this one.
            let deeper: Vec<usize> = heading_stack
                .keys()
                .filter(|k| **k >= level)
                .copied()
                .collect();
            for lv in deeper {
                heading_stack.remove(&lv);
            }
            heading_stack.insert(level, heading_text.clone());
            if levels.contains(&level) {
                // Start a new chunk at this heading.
                flush(
                    &mut chunks,
                    &current_lines,
                    &current_heading,
                    current_level,
                    &heading_stack,
                );
                current_lines = vec![line];
                current_heading = Some(heading_text);
                current_level = Some(level);
                continue;
            }
        }
        current_lines.push(line);
    }

    flush(
        &mut chunks,
        &current_lines,
        &current_heading,
        current_level,
        &heading_stack,
    );
    chunks
}
