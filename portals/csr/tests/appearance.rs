#![allow(clippy::unwrap_used, clippy::expect_used)]

#[test]
fn cockpit_shell_has_shared_appearance_control_and_prepaint_script() {
    let env = bss_csr::templating::build_environment();
    let html = env
        .get_template("cockpit_layout.html")
        .unwrap()
        .render(minijinja::context! { active_page => "sessions" })
        .unwrap();
    assert!(html.contains("data-appearance-picker"));
    assert!(html.contains("prefers-color-scheme:light"));
    assert!(html.find("appearance.js").unwrap() < html.find("<body").unwrap());
}

#[test]
fn branding_preview_scopes_both_modes_and_keeps_email_dark() {
    let dark = &bss_branding::THEMES[bss_branding::DEFAULT_THEME_ID];
    let light = bss_branding::themes::light_palette(dark);
    let env = bss_csr::templating::build_environment();
    let html = env
        .get_template("partials/branding_preview.html")
        .unwrap()
        .render(minijinja::context! { t => dark, light_t => light, name => "Example", mark => "$" })
        .unwrap();
    assert!(html.contains("Dark portal appearance"));
    assert!(html.contains("Light portal appearance"));
    assert!(html.contains(&format!("--bg:{}", dark.bg)));
    assert!(html.contains(&format!("--bg:{}", light.bg)));
    assert_eq!(html.matches("class=\"branding-preview-email\"").count(), 1);
}
