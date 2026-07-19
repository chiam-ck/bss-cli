//! Read-path contract for `bss_branding::config`: defaults on absence, mtime
//! hot-reload, last-good on parse error, env overrides, logo resolution. Port of
//! `packages/bss-branding/tests/test_config.py`.
//!
//! `current()` uses a process-global cache and the cases mutate process env, so
//! this is a single sequential test (parallel cases would race both). Each case
//! resets the cache + clears the env first, mirroring the Python autouse fixture.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use bss_branding::{current, reset_cache, BrandingSettings};

const PNG_BYTES: &[u8] = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00\x00\x00\x00\x00\
\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\
\x00\x00\x00\x00";

fn base_dir() -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("bss-branding-cfg-{nanos:x}"));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn case_dir(base: &Path, tag: &str) -> PathBuf {
    let dir = base.join(tag);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn write_settings(root: &Path, body: &str) -> PathBuf {
    let path = root.join("settings.toml");
    fs::write(&path, body).unwrap();
    path
}

fn bump_mtime(path: &Path) {
    let f = fs::File::options().write(true).open(path).unwrap();
    f.set_modified(SystemTime::now() + Duration::from_secs(10))
        .unwrap();
}

fn fresh() {
    for var in ["BSS_BRAND_NAME", "BSS_BRAND_THEME", "BSS_BRAND_MARK"] {
        std::env::remove_var(var);
    }
    reset_cache();
}

#[test]
fn read_path_contract() {
    let base = base_dir();

    // ── absent dir yields defaults, bootstraps nothing ──────────────────────
    fresh();
    let nowhere = base.join("nowhere");
    let view = current(Some(&nowhere));
    assert_eq!(view.brand_name, "bss-cli");
    assert_eq!(view.theme.id, "phosphor");
    assert_eq!(view.mark, "$");
    assert!(view.logo_path.is_none());
    assert_eq!(view.logo_version, 0);
    assert!(!nowhere.exists());

    // ── missing [branding] section yields defaults ──────────────────────────
    fresh();
    let dir = case_dir(&base, "missing-section");
    write_settings(&dir, "[llm]\ntemperature = 0.2\n");
    let view = current(Some(&dir));
    assert_eq!(view.brand_name, "bss-cli");
    assert_eq!(view.theme.id, "phosphor");

    // ── reads the [branding] section ────────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "reads");
    write_settings(
        &dir,
        "[branding]\nbrand_name = \"Kopi Mobile\"\ntheme = \"amber-crt\"\nmark = \"\u{25b2}\"\n",
    );
    let view = current(Some(&dir));
    assert_eq!(view.brand_name, "Kopi Mobile");
    assert_eq!(view.theme.id, "amber-crt");
    assert_eq!(view.mark, "\u{25b2}");

    // ── hot reload on mtime change ──────────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "reload");
    let path = write_settings(&dir, "[branding]\ntheme = \"ice\"\n");
    assert_eq!(current(Some(&dir)).theme.id, "ice");
    fs::write(&path, "[branding]\ntheme = \"magenta\"\n").unwrap();
    bump_mtime(&path);
    assert_eq!(current(Some(&dir)).theme.id, "magenta");

    // ── bad TOML keeps last good ────────────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "bad-toml");
    let path = write_settings(&dir, "[branding]\ntheme = \"ice\"\n");
    assert_eq!(current(Some(&dir)).theme.id, "ice");
    fs::write(&path, "[branding\nnot toml").unwrap();
    bump_mtime(&path);
    assert_eq!(current(Some(&dir)).theme.id, "ice");

    // ── unknown theme keeps last good ───────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "unknown-theme");
    let path = write_settings(&dir, "[branding]\ntheme = \"ice\"\n");
    assert_eq!(current(Some(&dir)).theme.id, "ice");
    fs::write(&path, "[branding]\ntheme = \"hotdog-stand\"\n").unwrap();
    bump_mtime(&path);
    assert_eq!(current(Some(&dir)).theme.id, "ice");

    // ── bad file with no prior good yields defaults ─────────────────────────
    fresh();
    let dir = case_dir(&base, "bad-no-prior");
    write_settings(&dir, "[branding\nnot toml");
    assert_eq!(current(Some(&dir)).theme.id, "phosphor");

    // ── env overrides ───────────────────────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "env");
    write_settings(&dir, "[branding]\nbrand_name = \"FileCo\"\n");
    std::env::set_var("BSS_BRAND_NAME", "EnvCo");
    std::env::set_var("BSS_BRAND_THEME", "paper");
    let view = current(Some(&dir));
    assert_eq!(view.brand_name, "EnvCo");
    assert_eq!(view.theme.id, "paper");
    assert_eq!(view.mark, "$"); // not overridden

    // ── invalid env override ignored ────────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "env-invalid");
    write_settings(&dir, "[branding]\ntheme = \"ice\"\n");
    std::env::set_var("BSS_BRAND_THEME", "hotdog-stand");
    assert_eq!(current(Some(&dir)).theme.id, "ice");

    // ── logo resolution ─────────────────────────────────────────────────────
    fresh();
    let dir = case_dir(&base, "logo");
    write_settings(&dir, "[branding]\nlogo_image = \"logo.png\"\n");
    let logo_dir = dir.join("branding");
    fs::create_dir_all(&logo_dir).unwrap();
    fs::write(logo_dir.join("logo.png"), PNG_BYTES).unwrap();
    let view = current(Some(&dir));
    assert_eq!(
        view.logo_path.as_deref(),
        Some(logo_dir.join("logo.png").as_path())
    );
    assert!(view.logo_version > 0);

    // ── logo configured but missing degrades ────────────────────────────────
    fresh();
    let dir = case_dir(&base, "logo-missing");
    write_settings(&dir, "[branding]\nlogo_image = \"logo.png\"\n");
    let view = current(Some(&dir));
    assert!(view.logo_path.is_none());
    assert_eq!(view.logo_version, 0);

    // ── file_settings rejects bad values (model-level) ──────────────────────
    assert!(BrandingSettings::validate("", "phosphor", "$", "").is_err());

    fresh();
    let _ = fs::remove_dir_all(&base);
}
