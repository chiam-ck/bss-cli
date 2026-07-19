//! Tool-row HTML — the single source of truth for how a tool result looks on the
//! browser surface. Port of `_render_tool_row_as_pre` in `bss_csr.routes.cockpit`.
//!
//! Used by **both** the page-load transcript path and the SSE stream, so the two
//! wires can't drift into different look-and-feel. Doctrine: tool results never
//! render as markdown / table / paraphrase — they render as monospace ASCII
//! inside `<pre>`.

use bss_cockpit::renderers::dispatch::render_tool_result;

/// Python's `html.escape(s)` — note `quote=True` is the **default**, so `"` and
/// `'` are escaped too.
fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// Render a tool-row body as a unified `<details open>` block.
///
/// The block opens by default — the operator's most recent question just landed,
/// so the answer should be immediately visible.
///
/// `body` is whatever was persisted in `cockpit.message`: pre-rendered ASCII (the
/// REPL stores the rendered card), raw JSON (a tool with no registered renderer),
/// or — preferred — ASCII produced by `render_tool_result` at stream time. When
/// the body **looks like JSON** we re-attempt rendering, so conversations stored
/// before the unified renderer landed retroactively get nice cards on reload.
///
/// **Newlines become `&#10;`** — that is not cosmetic. SSE's wire format requires
/// the `data:` field be a single physical line; a raw `\n` would split the frame
/// at the wrong boundary and drop every line of the card after the first. Inside
/// `<pre>` the browser parses `&#10;` back to a real LF, so the visible output is
/// identical to the REPL's.
///
/// Python keeps an `include_pill` parameter "for source compatibility" that no
/// longer affects output — both paths emit the same block, and the surrounding
/// stream logic decides whether to suppress a separate pill event. It is dropped
/// here rather than ported as a dead argument.
pub fn render_tool_row_as_pre(tool_name: &str, body: &str) -> String {
    let looks_like_json = {
        let t = body.trim_start();
        t.starts_with('{') || t.starts_with('[')
    };
    let rendered = if looks_like_json && !tool_name.is_empty() {
        render_tool_result(tool_name, body).unwrap_or_else(|| body.to_string())
    } else {
        body.to_string()
    };
    let escaped = html_escape(&rendered).replace('\n', "&#10;");
    let name_html = html_escape(if tool_name.is_empty() {
        "tool"
    } else {
        tool_name
    });
    format!(
        "<details class=\"tool-row\" open>\
         <summary class=\"tool-row-summary\">\
         <span class=\"tool-row-icon\">≈</span>\
         <span class=\"tool-row-name\">{name_html}</span>\
         </summary>\
         <pre class=\"tool-row-body\">{escaped}</pre>\
         </details>"
    )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn html_escape_matches_pythons_default_quote_true() {
        assert_eq!(html_escape("a & b"), "a &amp; b");
        assert_eq!(html_escape("<pre>"), "&lt;pre&gt;");
        // quote=True is Python's DEFAULT — both quote kinds escape.
        assert_eq!(html_escape("\"x\""), "&quot;x&quot;");
        assert_eq!(html_escape("it's"), "it&#x27;s");
    }

    /// The SSE contract: the emitted block must be ONE physical line, or the
    /// frame splits and every card line after the first is dropped.
    #[test]
    fn newlines_become_numeric_refs_so_the_frame_stays_one_line() {
        let out = render_tool_row_as_pre("x.y", "line1\nline2\nline3");
        assert!(
            !out.contains('\n'),
            "the block must not contain a raw newline"
        );
        assert!(out.contains("line1&#10;line2&#10;line3"));
    }

    /// Golden — the whole block, byte-for-byte from the oracle. Both wires (page
    /// load + SSE) emit this exact shape, so a drift here is a visible
    /// look-and-feel split between them.
    #[test]
    fn block_shape_matches_the_oracle_byte_for_byte() {
        assert_eq!(
            render_tool_row_as_pre("subscription.get", "┌─ box ─┐"),
            "<details class=\"tool-row\" open>\
             <summary class=\"tool-row-summary\">\
             <span class=\"tool-row-icon\">≈</span>\
             <span class=\"tool-row-name\">subscription.get</span>\
             </summary>\
             <pre class=\"tool-row-body\">┌─ box ─┐</pre>\
             </details>"
        );
    }

    /// A JSON body for a RENDERED tool is re-rendered on reload — that's what
    /// retro-fits nice cards onto conversations stored before the unified
    /// renderer landed.
    #[test]
    fn json_body_for_a_rendered_tool_is_re_rendered() {
        let out = render_tool_row_as_pre(
            "inventory.msisdn.count",
            r#"{"available":940,"reserved":5,"assigned":50,"ported_out":5,"total":1000}"#,
        );
        assert!(
            out.contains("MSISDN pool"),
            "should have re-rendered the card"
        );
        assert!(out.contains("940"));
    }

    /// A JSON body for an UNRENDERED tool surfaces verbatim — never a markdown
    /// table (the v0.19 doctrine).
    #[test]
    fn json_body_for_an_unrendered_tool_stays_raw() {
        let out = render_tool_row_as_pre("customer.create", r#"{"id":"CUST-1"}"#);
        assert!(out.contains("{&quot;id&quot;:&quot;CUST-1&quot;}"));
    }

    #[test]
    fn an_empty_tool_name_falls_back_to_tool() {
        let out = render_tool_row_as_pre("", "body");
        assert!(out.contains("<span class=\"tool-row-name\">tool</span>"));
    }

    /// HTML in a tool body must not escape into the page.
    #[test]
    fn body_html_is_escaped_not_injected() {
        let out = render_tool_row_as_pre("x.y", "<script>alert(1)</script>");
        assert!(!out.contains("<script>"));
        assert!(out.contains("&lt;script&gt;"));
    }
}
