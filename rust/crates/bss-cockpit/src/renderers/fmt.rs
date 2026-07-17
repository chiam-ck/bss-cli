//! Python format-spec primitives the renderers depend on.
//!
//! The renderers are dense with f-string specs (`{x:>6.1f}`, `{s:<10}`,
//! `{n:>3}`) plus `round()` and `str.title()`. Rust's equivalents differ in ways
//! that silently shift a column or a digit, so each one is isolated here and
//! golden-tested against the oracle:
//!
//! * **`round()` is banker's rounding.** Python rounds half to *even*
//!   (`round(2.5) == 2`, `round(3.5) == 4`); Rust's `f64::round` rounds half
//!   *away from zero* (`2.5f64.round() == 3.0`). A bundle sitting exactly on
//!   `x.5%` would render one percent off.
//! * **Padding counts characters**, not bytes (see [`super::boxes`]).
//! * **`str.title()`** upper-cases the first letter of each *word* and
//!   lower-cases the rest — `"data_roaming".title()` is `"Data_Roaming"`, not
//!   `"Data_roaming"`.

/// Python's `round()` — banker's rounding (half to even), returning an integer.
///
/// `round(0.5) == 0`, `round(1.5) == 2`, `round(2.5) == 2`, `round(-0.5) == 0`.
pub fn py_round(x: f64) -> i64 {
    let floor = x.floor();
    let diff = x - floor;
    let rounded = if (diff - 0.5).abs() < f64::EPSILON {
        // Exactly .5 → pick the even neighbour.
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        x.round()
    };
    rounded as i64
}

/// Python's `str.title()`: upper-case the first letter of each word, lower-case
/// the rest. A "word" boundary is any non-alphabetic character, so
/// `"data_roaming"` → `"Data_Roaming"` (the underscore starts a new word).
pub fn py_title(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut start_of_word = true;
    for c in s.chars() {
        if c.is_alphabetic() {
            if start_of_word {
                out.extend(c.to_uppercase());
            } else {
                out.extend(c.to_lowercase());
            }
            start_of_word = false;
        } else {
            out.push(c);
            start_of_word = true;
        }
    }
    out
}

/// `{s:<width}` — left-justify, padding with spaces. Char-wise; never truncates
/// (Python's alignment specs pad but do not cut).
pub fn ljust(s: &str, width: usize) -> String {
    let n = s.chars().count();
    if n >= width {
        s.to_string()
    } else {
        format!("{s}{}", " ".repeat(width - n))
    }
}

/// `{s:>width}` — right-justify, padding with spaces. Char-wise.
pub fn rjust(s: &str, width: usize) -> String {
    let n = s.chars().count();
    if n >= width {
        s.to_string()
    } else {
        format!("{}{s}", " ".repeat(width - n))
    }
}

/// `{x:>width.prec f}` — fixed-point, right-justified.
pub fn rjust_f(x: f64, width: usize, prec: usize) -> String {
    rjust(&format!("{x:.prec$}"), width)
}

/// Python `s[:n]` — truncate to `n` characters (never bytes).
pub fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Python `repr()` of a string: single quotes unless the string contains a `'`
/// and no `"`; escapes backslash, the quote, and `\n`/`\r`/`\t`.
///
/// Used by the case renderer's `{subject!r:<40}` title.
pub fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    /// Golden — Python's round() is banker's rounding. Rust's f64::round is not.
    #[test]
    fn py_round_is_bankers() {
        assert_eq!(py_round(0.5), 0);
        assert_eq!(py_round(1.5), 2);
        assert_eq!(py_round(2.5), 2);
        assert_eq!(py_round(3.5), 4);
        assert_eq!(py_round(4.5), 4);
        // Non-half values behave normally.
        assert_eq!(py_round(2.4), 2);
        assert_eq!(py_round(2.6), 3);
        assert_eq!(py_round(0.0), 0);
        assert_eq!(py_round(-0.5), 0);
        assert_eq!(py_round(-1.5), -2);
        // The divergence Rust would introduce, spelled out.
        assert_ne!(py_round(2.5), 2.5f64.round() as i64);
    }

    #[test]
    fn py_title_matches_oracle() {
        assert_eq!(py_title("data"), "Data");
        // The underscore starts a new word — NOT "Data_roaming".
        assert_eq!(py_title("data_roaming"), "Data_Roaming");
        assert_eq!(py_title("voice_minutes"), "Voice_Minutes");
        assert_eq!(py_title("SMS"), "Sms");
        assert_eq!(py_title(""), "");
        assert_eq!(py_title("?"), "?");
    }

    #[test]
    fn justify_is_char_wise() {
        assert_eq!(ljust("ab", 4), "ab  ");
        assert_eq!(rjust("ab", 4), "  ab");
        // Over-width passes through un-truncated, like Python.
        assert_eq!(ljust("abcdef", 3), "abcdef");
        assert_eq!(rjust("abcdef", 3), "abcdef");
        // Multi-byte pads by chars, not bytes.
        assert_eq!(ljust("●", 3).chars().count(), 3);
        assert_eq!(rjust("●", 3).chars().count(), 3);
    }

    #[test]
    fn rjust_f_matches_oracle() {
        // `f"{used:>6.1f}"`
        assert_eq!(rjust_f(5.0, 6, 1), "   5.0");
        assert_eq!(rjust_f(1024.0, 6, 1), "1024.0");
        assert_eq!(rjust_f(0.0, 6, 1), "   0.0");
        assert_eq!(rjust_f(12345.67, 6, 1), "12345.7");
    }

    #[test]
    fn truncate_is_char_wise() {
        assert_eq!(truncate("abcdef", 3), "abc");
        assert_eq!(truncate("abc", 10), "abc");
        assert_eq!(truncate("●●●●", 2), "●●");
    }

    #[test]
    fn py_repr_str_quote_selection() {
        assert_eq!(py_repr_str("hi"), "'hi'");
        assert_eq!(py_repr_str("it's"), "\"it's\"");
        assert_eq!(py_repr_str(""), "''");
    }
}
