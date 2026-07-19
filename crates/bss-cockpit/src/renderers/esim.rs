//! eSIM activation renderer — ICCID/IMSI/MSISDN + an ASCII QR of the LPA string.
//! Port of `bss_cockpit.renderers.esim`.
//!
//! # The QR is a documented parity seam (R2-class)
//!
//! **The QR *modules* are NOT byte-identical to the oracle's, by design.**
//! python-qrcode and Rust's `qrcode` crate encode the same LPA payload into
//! different matrices: they segment the string into QR modes differently (so the
//! bitstream, and therefore the codewords, differ) *and* they pick different mask
//! patterns. Forcing the mask is not enough — the underlying data differs too.
//!
//! Both outputs are **valid QR codes that scan to the identical LPA string**, and
//! both libraries select the same *version* for a given payload, so the block has
//! the same dimensions and the card's line count and frame are unchanged. The
//! divergence is confined to which cells inside the QR are dark.
//!
//! Chasing byte-parity would mean reimplementing python-qrcode's segmentation
//! optimizer and its `lost_point` mask-penalty scoring on top of the `qrcode`
//! crate's canvas API — a few hundred lines of brittle mimicry of another
//! library's internals, to make a scannable square render identical pixels.
//! Accepted as a semantically-null divergence (human call, 2026-07-15).
//!
//! The tests reflect exactly that: everything outside the QR block is
//! byte-golden; the QR block is asserted on its **functional** contract (same
//! dimensions, only block glyphs) rather than pixel equality. No test claims
//! parity it doesn't have.

use qrcode::{EcLevel, QrCode};
use serde_json::Value;

use super::boxes::{format_iccid, format_msisdn};
use super::fmt::{ljust, py_or, truncate};

const WIDTH: usize = 64;

fn status_strip(status: &str) -> String {
    match status.to_lowercase().as_str() {
        "prepared" => "● PREPARED".to_string(),
        "downloaded" => "● DOWNLOADED".to_string(),
        "activated" => "● ACTIVATED".to_string(),
        "suspended" => "○ SUSPENDED".to_string(),
        "released" => "○ RELEASED".to_string(),
        "recycled" => "○ RECYCLED".to_string(),
        _ => format!("● {}", status.to_uppercase()),
    }
}

/// Show the last 4 digits behind a dot prefix unless `show_full`.
///
/// The card is often shown to humans (CSR screens, demo screenshots) and these
/// identifiers must not leak past last-4 in those contexts (CLAUDE.md: never log
/// full ICCIDs beyond last-4).
fn redact_id(value: &str, show_full: bool) -> String {
    if show_full || value.is_empty() || value == "—" {
        return value.to_string();
    }
    let n = value.chars().count();
    if n <= 4 {
        return value.to_string();
    }
    let tail: String = value.chars().skip(n - 4).collect();
    format!("{}{tail}", "•".repeat(n - 4))
}

/// The QR as ASCII rows using two-row block characters.
///
/// Pairing two QR rows into one terminal row halves the vertical size. Each
/// terminal row encodes two QR rows: top half (`▀`), bottom half (`▄`), both
/// (`█`), neither (space).
fn qr_ascii(payload: &str, border: usize) -> Vec<String> {
    let Ok(code) = QrCode::with_error_correction_level(payload, EcLevel::M) else {
        return Vec::new();
    };
    let modules = code.width();
    let colors = code.to_colors();
    let size = modules + border * 2;
    // Build the bordered matrix (the crate's `to_colors` excludes the quiet zone;
    // python-qrcode's `get_matrix` includes it).
    let dark = |r: usize, c: usize| -> bool {
        if r < border || c < border || r >= border + modules || c >= border + modules {
            return false;
        }
        colors[(r - border) * modules + (c - border)] == qrcode::Color::Dark
    };
    // Pad with an empty bottom row so the pairs line up.
    let rows = if size % 2 == 1 { size + 1 } else { size };
    let mut lines = Vec::with_capacity(rows / 2);
    for r in (0..rows).step_by(2) {
        let line: String = (0..size)
            .map(|c| {
                let t = dark(r, c);
                let b = r + 1 < size && dark(r + 1, c);
                match (t, b) {
                    (true, true) => '█',
                    (true, false) => '▀',
                    (false, true) => '▄',
                    (false, false) => ' ',
                }
            })
            .collect();
        lines.push(line);
    }
    lines
}

/// Render the eSIM activation card with an ASCII QR.
///
/// Expects `activation` shaped like `{iccid, imsi, msisdn, activationCode,
/// status?}`. `show_full` reveals the full ICCID + IMSI; the default redacts to
/// last-4.
pub fn render_esim_activation(activation: &Value, show_full: bool) -> String {
    let iccid = py_or(activation, &["iccid"], "—");
    let imsi = py_or(activation, &["imsi"], "—");
    let msisdn = py_or(activation, &["msisdn"], "");
    let code = py_or(activation, &["activationCode"], "");
    let status = py_or(activation, &["status"], "prepared");

    let lpa = if code.starts_with("LPA:") {
        code.clone()
    } else if !code.is_empty() {
        format!("LPA:1${code}")
    } else {
        String::new()
    };

    let qr_payload = if lpa.is_empty() {
        "LPA:1$smdp.bss-cli.local$UNKNOWN"
    } else {
        &lpa
    };
    let qr_lines = qr_ascii(qr_payload, 1);
    let qr_width = qr_lines
        .iter()
        .map(|l| l.chars().count())
        .max()
        .unwrap_or(0);

    let iccid_disp = if show_full {
        format_iccid(&iccid)
    } else {
        redact_id(&iccid, show_full)
    };
    let imsi_disp = if !imsi.is_empty() && imsi.chars().count() >= 5 && show_full {
        let c: Vec<char> = imsi.chars().collect();
        format!(
            "{} {} {}",
            c[..3].iter().collect::<String>(),
            c[3..5].iter().collect::<String>(),
            c[5..].iter().collect::<String>()
        )
        .trim()
        .to_string()
    } else {
        redact_id(&imsi, show_full)
    };

    let inner = WIDTH - 2;
    let row = |s: &str| -> String { format!("│{}│", ljust(s, inner)) };

    let title = format!("eSIM Activation  {}", status_strip(&status));
    let mut out = vec![format!(
        "┌─ {title} {}┐",
        "─".repeat(inner.saturating_sub(title.chars().count() + 3))
    )];
    out.push(row(""));
    out.push(row(&format!("  ICCID    {iccid_disp}")));
    out.push(row(&format!("  IMSI     {imsi_disp}")));
    out.push(row(&format!("  MSISDN   {}", format_msisdn(&msisdn))));
    out.push(row(""));

    // QR block, centred with a side label.
    out.push(row("  Scan with your device camera:"));
    out.push(row(""));
    let pad_left = ((inner.saturating_sub(qr_width)) / 2).max(2);
    for q in &qr_lines {
        let line = format!("{}{q}", " ".repeat(pad_left));
        out.push(row(&truncate(&line, inner)));
    }

    out.push(row(""));
    out.push(row("  Or enter the LPA code manually:"));
    out.push(row(&format!("  {}", truncate(&lpa, inner - 4))));
    if !show_full {
        out.push(row(""));
        out.push(row("  (use --show-full to reveal full ICCID + IMSI)"));
    }
    out.push(format!("└{}┘", "─".repeat(inner)));
    out.join("\n")
}
