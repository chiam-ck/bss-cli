//! Dump browser CSS for the optional real-browser appearance test.
fn main() {
    for theme in bss_branding::THEMES.values() {
        println!("{}\t{}", theme.id, bss_branding::portal_branding_css(theme));
    }
}
