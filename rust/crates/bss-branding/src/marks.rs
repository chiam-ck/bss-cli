//! Built-in logo marks + validation for operator-typed custom marks. Port of
//! `bss_branding.marks`.
//!
//! The mark is the text glyph that renders wherever an image can't (CLI banner,
//! email headers, either portal with no logo uploaded). It is operator input
//! that flows into hand-built email HTML, so validation here is a security
//! boundary — HTML-active characters are rejected outright, on top of escaping
//! at render time.

/// The built-in marks offered in the picker.
pub const LOGO_MARKS: &[&str] = &["$", "\u{25cf}", "\u{25b2}", "\u{2726}", "\u{25ba}"];

const FORBIDDEN_CHARS: &[char] = &['<', '>', '&', '"', '\''];

/// Return the stripped mark or an error message.
///
/// 1–3 printable characters; HTML-active characters (`<`, `>`, `&`, quotes) are
/// rejected outright.
///
/// `isprintable` parity note: Python's `str.isprintable()` treats Unicode "Other"
/// (C*) and "Separator" (Z*) categories as non-printable, excepting ASCII space.
/// We approximate that as "not a control char and not whitespace, except plain
/// space" — exact for every tested mark (glyphs, `$`, control/whitespace
/// rejection). The only divergence is exotic format chars (Cf, e.g. zero-width
/// joiners), which Rust's `char::is_control` doesn't flag; a 1–3 char logo mark
/// never legitimately contains one, and the forbidden-char gate is unaffected.
pub fn validate_mark(value: &str) -> Result<String, String> {
    let mark = value.trim().to_string();
    let len = mark.chars().count();
    if !(1..=3).contains(&len) {
        return Err("mark must be 1-3 characters".to_string());
    }
    if !mark.chars().all(is_printable) {
        return Err("mark must be printable characters only".to_string());
    }
    if mark.chars().any(|c| FORBIDDEN_CHARS.contains(&c)) {
        return Err("mark must not contain <, >, &, quotes".to_string());
    }
    Ok(mark)
}

fn is_printable(c: char) -> bool {
    c == ' ' || (!c.is_control() && !c.is_whitespace())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn accepts_glyphs_and_dollar() {
        assert_eq!(validate_mark("$").unwrap(), "$");
        assert_eq!(validate_mark("\u{25b2}").unwrap(), "\u{25b2}"); // ▲
        assert_eq!(validate_mark("  ▲  ").unwrap(), "▲");
        for m in LOGO_MARKS {
            assert_eq!(&validate_mark(m).unwrap(), m);
        }
    }

    #[test]
    fn rejects_bad_marks() {
        assert!(validate_mark("").is_err());
        assert!(validate_mark("   ").is_err());
        assert!(validate_mark("toolong").is_err());
        assert!(validate_mark("<b>").is_err());
        assert!(validate_mark("a&b").is_err());
        assert!(validate_mark("\t").is_err());
    }
}
