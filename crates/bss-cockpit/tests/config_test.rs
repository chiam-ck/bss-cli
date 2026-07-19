//! Config loader behaviour: parse, autobootstrap, mtime reload, last-good
//! fallback. Pure (temp dir, no DB) — runs in CI.
//!
//! `current()` uses a process-global cache, so this is a single sequential test
//! (parallel cases would race the cache + the different roots).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs;
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use bss_cockpit::{config, OPERATOR_ACTOR};

fn scratch_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "bss-cockpit-cfg-{tag}-{:08x}",
        rand::random::<u32>()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn bump_mtime(path: &PathBuf, secs: u64) {
    let f = fs::File::options().write(true).open(path).unwrap();
    f.set_modified(SystemTime::now() + Duration::from_secs(secs))
        .unwrap();
}

#[test]
fn config_loader_behaviour() {
    assert_eq!(OPERATOR_ACTOR, "operator");

    // ── 1. explicit files parse into the expected sections ──────────────────
    let dir = scratch_dir("parse");
    fs::write(dir.join("OPERATOR.md"), "# My persona\n\nHouse rules.\n").unwrap();
    fs::write(
        dir.join("settings.toml"),
        r#"
[llm]
model = "deepseek/deepseek-v4-pro"
temperature = 0.4

[cockpit]
allow_destructive_default = true

[ports]
csr_portal = 9099

[dev_service_urls]
catalog = "http://localhost:8001"

[branding]
brand_name = "acme"
"#,
    )
    .unwrap();

    config::reset_cache();
    let cfg = config::current(Some(&dir)).expect("load config");
    assert_eq!(cfg.operator_md, "# My persona\n\nHouse rules.\n");
    assert_eq!(
        cfg.settings.llm.model.as_deref(),
        Some("deepseek/deepseek-v4-pro")
    );
    assert_eq!(cfg.settings.llm.temperature, 0.4);
    assert!(cfg.settings.cockpit.allow_destructive_default);
    assert_eq!(cfg.settings.ports.csr_portal, 9099);
    assert_eq!(
        cfg.settings
            .dev_service_urls
            .get("catalog")
            .map(String::as_str),
        Some("http://localhost:8001")
    );
    // The [branding] table is ignored (deferred to P6) — it must not fail load.

    // ── 2. cache hit: second call without change returns the same snapshot ──
    let cfg2 = config::current(Some(&dir)).expect("load config again");
    assert_eq!(
        cfg.last_loaded_at, cfg2.last_loaded_at,
        "expected cache hit"
    );

    // ── 3. last-good fallback on invalid TOML (bump mtime to force a reload) ─
    fs::write(dir.join("settings.toml"), "this = is = not valid toml\n").unwrap();
    bump_mtime(&dir.join("settings.toml"), 10);
    let cfg3 = config::current(Some(&dir)).expect("serve last-good on bad toml");
    assert_eq!(
        cfg3.settings.ports.csr_portal, 9099,
        "invalid toml should serve the last-good view"
    );

    // ── 4. valid reload picks up the new value ──────────────────────────────
    fs::write(dir.join("settings.toml"), "[ports]\ncsr_portal = 9100\n").unwrap();
    bump_mtime(&dir.join("settings.toml"), 20);
    let cfg4 = config::current(Some(&dir)).expect("reload valid toml");
    assert_eq!(cfg4.settings.ports.csr_portal, 9100);
    // Defaults fill the missing sections.
    assert_eq!(cfg4.settings.llm.temperature, 0.2);

    // ── 5. autobootstrap: an empty dir materializes the embedded defaults ───
    let empty = scratch_dir("bootstrap");
    config::reset_cache();
    let cfg5 = config::current(Some(&empty)).expect("autobootstrap defaults");
    assert!(empty.join("OPERATOR.md").exists());
    assert!(empty.join("settings.toml").exists());
    assert!(cfg5.operator_md.contains("Operator persona"));

    // cleanup
    let _ = fs::remove_dir_all(&dir);
    let _ = fs::remove_dir_all(&empty);
}
