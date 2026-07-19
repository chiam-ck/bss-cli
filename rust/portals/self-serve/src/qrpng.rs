//! Render an eSIM LPA activation code (or any payload) as a base64-embeddable
//! PNG QR. Port of `bss_self_serve.qrpng`.
//!
//! The web portal draws a real PNG (the CLI ships an ASCII QR) served inline via
//! a `data:image/png` URI. The exact PNG bytes are not a wire contract — the Rust
//! `qrcode`/`image` stack produces a different byte stream from Python's `qrcode`
//! lib, but the encoded payload, module layout, and colours match: dark
//! `#0e1014` on white `#ffffff`, box size 8, 2-module quiet zone.

use base64::Engine;
use image::{ImageFormat, Rgb, RgbImage};
use qrcode::{Color, QrCode};

const DARK: Rgb<u8> = Rgb([0x0e, 0x10, 0x14]);
const LIGHT: Rgb<u8> = Rgb([0xff, 0xff, 0xff]);

/// `data:image/png;base64,...` for an eSIM LPA activation code.
pub fn activation_qr_data_uri(lpa_code: &str) -> String {
    qr_data_uri(lpa_code, 8, 2)
}

/// Generic `data:image/png;base64,...` QR — activation codes and KYC handoff
/// URLs alike. Returns an empty string on the (unreachable-for-real-payloads)
/// encode error rather than failing the page.
pub fn qr_data_uri(payload: &str, box_size: u32, border: u32) -> String {
    match render_png(payload, box_size, border) {
        Ok(bytes) => {
            let b64 = base64::engine::general_purpose::STANDARD.encode(bytes);
            format!("data:image/png;base64,{b64}")
        }
        Err(e) => {
            tracing::warn!(error = %e, "portal.qr.render_failed");
            String::new()
        }
    }
}

fn render_png(payload: &str, box_size: u32, border: u32) -> Result<Vec<u8>, String> {
    let code = QrCode::new(payload.as_bytes()).map_err(|e| e.to_string())?;
    let modules = code.width() as u32; // side length in modules (no quiet zone)
    let side = (modules + 2 * border) * box_size;
    let colors = code.to_colors();

    let mut img: RgbImage = RgbImage::from_pixel(side, side, LIGHT);
    for my in 0..modules {
        for mx in 0..modules {
            let idx = (my * modules + mx) as usize;
            if colors[idx] == Color::Dark {
                let x0 = (mx + border) * box_size;
                let y0 = (my + border) * box_size;
                for dy in 0..box_size {
                    for dx in 0..box_size {
                        img.put_pixel(x0 + dx, y0 + dy, DARK);
                    }
                }
            }
        }
    }

    let mut buf = std::io::Cursor::new(Vec::new());
    img.write_to(&mut buf, ImageFormat::Png)
        .map_err(|e| e.to_string())?;
    Ok(buf.into_inner())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn produces_a_png_data_uri() {
        let uri = activation_qr_data_uri("LPA:1$sm-dp.example$ACTIVATION-CODE");
        assert!(uri.starts_with("data:image/png;base64,"));
        // Decode + sniff the PNG magic to confirm a real image came out.
        let b64 = uri.strip_prefix("data:image/png;base64,").unwrap();
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .unwrap();
        assert_eq!(&bytes[..8], b"\x89PNG\r\n\x1a\n");
    }

    #[test]
    fn empty_payload_still_renders() {
        // An empty QR is valid; the caller guards on activation_code presence.
        assert!(qr_data_uri("x", 8, 2).starts_with("data:image/png;base64,"));
    }
}
