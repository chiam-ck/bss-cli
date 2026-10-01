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
