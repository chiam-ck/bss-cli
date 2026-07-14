//! Logo-image validation: magic-byte sniffing + size cap. Port of
//! `bss_branding.assets`.
//!
//! The upload route trusts nothing the browser says — not the filename, not the
//! Content-Type header, not Content-Length. The bytes decide. PNG, JPEG and
//! WebP only; **never SVG** (scriptable → XSS on both portals). 256 KB cap.

/// 256 KB.
pub const MAX_LOGO_BYTES: usize = 262_144;

/// A sniffed image type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImageType {
    Png,
    Jpeg,
    Webp,
}

impl ImageType {
    /// The fixed on-disk filename under `.bss-cli/branding/`. Fixed names are the
    /// anti-path-traversal story: no user-controlled path component ever reaches
    /// the filesystem.
    pub fn filename(self) -> &'static str {
        match self {
            ImageType::Png => "logo.png",
            ImageType::Jpeg => "logo.jpg",
            ImageType::Webp => "logo.webp",
        }
    }

    /// The HTTP `Content-Type` for this type.
    pub fn content_type(self) -> &'static str {
        match self {
            ImageType::Png => "image/png",
            ImageType::Jpeg => "image/jpeg",
            ImageType::Webp => "image/webp",
        }
    }
}

/// The three legal fixed logo filenames, in `ImageType` order.
pub const LOGO_FILENAMES: &[&str] = &["logo.png", "logo.jpg", "logo.webp"];

/// Reverse map for serving: fixed filename → content type. `None` for anything
/// that isn't one of the three legal names.
pub fn content_type_by_filename(filename: &str) -> Option<&'static str> {
    match filename {
        "logo.png" => Some("image/png"),
        "logo.jpg" => Some("image/jpeg"),
        "logo.webp" => Some("image/webp"),
        _ => None,
    }
}

/// `true` if `filename` is one of the three legal fixed logo names (the
/// `logo_image` settings field's allowlist).
pub fn is_legal_logo_filename(filename: &str) -> bool {
    content_type_by_filename(filename).is_some()
}

/// Identify PNG/JPEG/WebP by magic bytes; `None` for anything else (incl. SVG).
pub fn sniff_image_type(data: &[u8]) -> Option<ImageType> {
    if data.len() >= 8 && data[..8] == *b"\x89PNG\r\n\x1a\n" {
        return Some(ImageType::Png);
    }
    if data.len() >= 3 && data[..3] == *b"\xff\xd8\xff" {
        return Some(ImageType::Jpeg);
    }
    if data.len() >= 12 && data[..4] == *b"RIFF" && data[8..12] == *b"WEBP" {
        return Some(ImageType::Webp);
    }
    None
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn accepts_png_jpeg_webp() {
        let png = [b"\x89PNG\r\n\x1a\n".as_slice(), &[0u8; 16]].concat();
        let jpeg = [b"\xff\xd8\xff\xe0".as_slice(), &[0u8; 16]].concat();
        let webp = [b"RIFF\x24\x00\x00\x00WEBP".as_slice(), &[0u8; 16]].concat();
        assert_eq!(sniff_image_type(&png), Some(ImageType::Png));
        assert_eq!(sniff_image_type(&jpeg), Some(ImageType::Jpeg));
        assert_eq!(sniff_image_type(&webp), Some(ImageType::Webp));
    }

    #[test]
    fn rejects_svg_gif_garbage_and_truncated() {
        let svg = b"<svg xmlns=\"http://www.w3.org/2000/svg\"><script>x</script></svg>";
        assert_eq!(sniff_image_type(svg), None);
        let gif = [b"GIF89a".as_slice(), &[0u8; 16]].concat();
        assert_eq!(sniff_image_type(&gif), None);
        assert_eq!(sniff_image_type(b"hello world"), None);
        assert_eq!(sniff_image_type(b""), None);
        assert_eq!(sniff_image_type(b"\x89PN"), None); // truncated PNG magic
        assert_eq!(sniff_image_type(b"RIFF\x00\x00\x00\x00WAVE"), None); // RIFF, not WEBP
    }

    #[test]
    fn cap_is_256k() {
        assert_eq!(MAX_LOGO_BYTES, 262_144);
    }

    #[test]
    fn filename_and_content_type_maps() {
        assert_eq!(ImageType::Png.filename(), "logo.png");
        assert_eq!(ImageType::Jpeg.filename(), "logo.jpg");
        assert_eq!(ImageType::Webp.filename(), "logo.webp");
        assert_eq!(content_type_by_filename("logo.jpg"), Some("image/jpeg"));
        assert_eq!(content_type_by_filename("logo.svg"), None);
        assert!(!is_legal_logo_filename("../etc/passwd"));
    }
}
