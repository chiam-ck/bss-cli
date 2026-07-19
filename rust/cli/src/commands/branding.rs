//! `bss branding ...` — operator branding from the terminal (v1.8). Port of
//! `cli/bss_cli/commands/branding.py`. Thin verbs over the same seams the cockpit
//! Branding screen uses: reads via `bss_branding` (file values, no env overrides),
//! writes via `bss_cockpit::write_branding_settings` — the validation gate. No
//! business logic here; the logo image upload stays on the cockpit screen.
//!
//! The colour swatches Python renders with `rich.Text` styles, and its `rich.Table`
//! layouts, are ANSI-truecolor presentation and can't byte-match — `show`/`themes`
//! render the field text with the palette hexes inline (a documented CLI seam). The
//! doctrine-meaningful part — the write passes through `BrandingSettings::validate`
//! then `write_branding_settings` — is faithful.

use std::process::ExitCode;

use bss_branding::{BrandingSettings, THEMES};
use clap::{Args, Subcommand};

#[derive(Args)]
pub struct BrandingArgs {
    #[command(subcommand)]
    command: BrandingCommand,
}

#[derive(Subcommand)]
enum BrandingCommand {
    /// Current branding (as resolved, env overrides applied).
    Show,
    /// List the available theme palettes.
    Themes,
    /// Switch the color scheme (portals, emails, REPL banner).
    SetTheme { theme_id: String },
    /// Set the operator name shown in headers, emails, chat and banner.
    SetName { name: String },
    /// Set the text logo mark (used everywhere an image can't render).
    SetMark { mark: String },
}

pub fn run(args: BrandingArgs) -> ExitCode {
    match args.command {
        BrandingCommand::Show => show(),
        BrandingCommand::Themes => themes(),
        BrandingCommand::SetTheme { theme_id } => set_theme(theme_id),
        BrandingCommand::SetName { name } => set_name(name),
        BrandingCommand::SetMark { mark } => set_mark(mark),
    }
}

fn show() -> ExitCode {
    let view = bss_branding::current(None);
    println!("operator name   {}", view.brand_name);
    println!("theme           {}  ({})", view.theme.id, view.theme.label);
    println!("                {}", swatch(view.theme.id));
    println!("mark            {}", view.mark);
    let logo = match &view.logo_path {
        Some(p) => p.display().to_string(),
        None => "(none — headers use the mark)".to_string(),
    };
    println!("logo image      {logo}");
    ExitCode::SUCCESS
}

fn themes() -> ExitCode {
    let active = bss_branding::current(None).theme.id;
    println!("id            label                       palette");
    for t in THEMES.values() {
        let marker = if t.id == active { " ← active" } else { "" };
        println!(
            "{:<13} {:<27} {}",
            t.id,
            format!("{}{marker}", t.label),
            swatch(t.id)
        );
    }
    ExitCode::SUCCESS
}

fn set_theme(theme_id: String) -> ExitCode {
    if !THEMES.contains_key(theme_id.as_str()) {
        let known = THEMES.keys().copied().collect::<Vec<_>>().join(", ");
        eprintln!("unknown theme '{theme_id}' — pick one of: {known}");
        return ExitCode::from(1);
    }
    if let Err(code) = save(|s| s.theme = theme_id.clone()) {
        return code;
    }
    println!("theme → {theme_id}  {}", swatch(&theme_id));
    ExitCode::SUCCESS
}

fn set_name(name: String) -> ExitCode {
    if let Err(code) = save(|s| s.brand_name = name.clone()) {
        return code;
    }
    println!("operator name → {name}");
    ExitCode::SUCCESS
}

fn set_mark(mark: String) -> ExitCode {
    if let Err(code) = save(|s| s.mark = mark.clone()) {
        return code;
    }
    println!("mark → {mark}");
    ExitCode::SUCCESS
}

/// `_save(**fields)` — read the file settings, apply the update, validate through the
/// same gate the cockpit route uses, then persist. A rejection prints `rejected: …`
/// and exits 1 (Python catches `ValidationError`/`ValueError`).
fn save(update: impl FnOnce(&mut BrandingSettings)) -> Result<(), ExitCode> {
    let mut s = bss_branding::file_settings(None);
    update(&mut s);
    let validated = BrandingSettings::validate(&s.brand_name, &s.theme, &s.mark, &s.logo_image)
        .map_err(|e| {
            eprintln!("rejected: {e}");
            ExitCode::from(1)
        })?;
    bss_cockpit::write_branding_settings(&validated, None).map_err(|e| {
        eprintln!("rejected: {e}");
        ExitCode::from(1)
    })?;
    Ok(())
}

/// The palette as inline hexes (accent/fg/bg_elev/accent_amber) — a text stand-in for
/// Python's coloured `rich.Text` blocks, which are ANSI-truecolor and can't byte-match.
fn swatch(theme_id: &str) -> String {
    match THEMES.get(theme_id) {
        Some(t) => format!("{} {} {} {}", t.accent, t.fg, t.bg_elev, t.accent_amber),
        None => String::new(),
    }
}
