//! Framework-free helper for serving the uploaded logo. Port of
//! `bss_branding.web` with the Starlette `Response` replaced by a plain data
//! struct so this core crate stays web-framework-free (the P6 axum portal wraps
//! it into an `axum::response::Response`).

use crate::assets::content_type_by_filename;
use crate::config::current;

/// The bytes + headers a portal needs to serve the current logo.
#[derive(Debug, Clone)]
pub struct LogoHttp {
    pub bytes: Vec<u8>,
    pub content_type: &'static str,
    /// `public, max-age=31536000, immutable` — safe because the URL carries
    /// `?v=<mtime>`, so a replaced logo gets a new URL.
    pub cache_control: &'static str,
    pub etag: String,
}

/// `None` when no logo is configured/present (the portal returns 404); else the
/// image bytes + caching headers.
pub fn logo_http() -> Option<LogoHttp> {
    let view = current(None);
    let path = view.logo_path?;
    let bytes = std::fs::read(&path).ok()?;
    let content_type = path
        .file_name()
        .and_then(|n| n.to_str())
        .and_then(content_type_by_filename)
        .unwrap_or("application/octet-stream");
    let etag = format!("\"{}-{}\"", view.logo_version, bytes.len());
    Some(LogoHttp {
        bytes,
        content_type,
        cache_control: "public, max-age=31536000, immutable",
        etag,
    })
}
