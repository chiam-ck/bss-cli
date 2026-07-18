//! `bss scenario ...` — YAML scenario runner. Port of `cli/bss_cli/commands/scenario.py`.
//!
//! **This slice:** `validate` (parse every file, report errors) and `list` (enumerate
//! a directory with tags + step count). `run` / `run-all` land with the executor in
//! the following slices — they're declared here so `--help` shows the full surface,
//! and print a not-yet-available notice until then.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, Subcommand};
use glob::glob;

use crate::scenarios::load_scenario;

#[derive(Args)]
pub struct ScenarioArgs {
    #[command(subcommand)]
    command: ScenarioCommand,
}

#[derive(Subcommand)]
enum ScenarioCommand {
    /// Parse each scenario file; exit non-zero if any fail.
    Validate {
        /// YAML file(s) to validate.
        #[arg(required = true)]
        paths: Vec<PathBuf>,
    },
    /// List scenarios in a directory with tags and step count.
    List {
        /// Directory of scenario YAML files.
        #[arg(default_value = "scenarios")]
        directory: PathBuf,
    },
    /// Run a single scenario file.
    Run {
        /// Scenario YAML file.
        path: PathBuf,
        /// Fail `ask:` steps immediately.
        #[arg(long = "no-llm")]
        no_llm: bool,
        /// Force every step through the LLM (Task #6).
        #[arg(long = "via-llm")]
        via_llm: bool,
    },
    /// Run every scenario in a directory and print a summary.
    RunAll {
        /// Directory of scenario YAML files.
        #[arg(default_value = "scenarios")]
        directory: PathBuf,
        /// Skip `ask:` steps — fail fast.
        #[arg(long = "no-llm")]
        no_llm: bool,
        /// Only run scenarios tagged with this value.
        #[arg(long)]
        tag: Option<String>,
    },
}

pub async fn run(args: ScenarioArgs) -> ExitCode {
    match args.command {
        ScenarioCommand::Validate { paths } => validate(&paths),
        ScenarioCommand::List { directory } => list(&directory),
        ScenarioCommand::Run { .. } | ScenarioCommand::RunAll { .. } => {
            eprintln!(
                "scenario run/run-all is not wired yet — this slice ships validate + list; \
                 the executor lands in the next slice."
            );
            ExitCode::from(2)
        }
    }
}

/// `bss scenario validate <path>...` — parse each file, print ✓/✗, exit 1 on any fail.
fn validate(paths: &[PathBuf]) -> ExitCode {
    let mut failed = 0u32;
    for path in paths {
        match load_scenario(path) {
            Ok(scenario) => println!(
                "✓ {} — {} ({} steps)",
                path.display(),
                scenario.name,
                scenario.steps.len()
            ),
            Err(e) => {
                failed += 1;
                println!("✗ {}", path.display());
                println!("  {e}");
            }
        }
    }
    if failed > 0 {
        ExitCode::from(1)
    } else {
        ExitCode::SUCCESS
    }
}

/// `bss scenario list <dir>` — table of `file / name / tags / steps`. The box-drawing
/// chrome Python's `rich.Table` draws is a documented CLI seam; the per-row cells match.
fn list(directory: &Path) -> ExitCode {
    if !directory.is_dir() {
        eprintln!("not a directory: {}", directory.display());
        return ExitCode::from(2);
    }
    let files = scenario_files(directory);
    if files.is_empty() {
        println!("no YAML files in {}", directory.display());
        return ExitCode::SUCCESS;
    }
    println!("scenarios in {}", directory.display());
    println!("{:<44} {:<34} {:<20} steps", "file", "name", "tags");
    for path in files {
        let file = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        match load_scenario(&path) {
            Ok(s) => {
                let tags = if s.tags.is_empty() {
                    "—".to_string()
                } else {
                    s.tags.join(", ")
                };
                println!("{file:<44} {:<34} {tags:<20} {}", s.name, s.steps.len());
            }
            Err(e) => {
                let snippet: String = e.chars().take(60).collect();
                println!("{file:<44} {:<34} {:<20} {snippet}", "INVALID", "");
            }
        }
    }
    ExitCode::SUCCESS
}

/// `*.yaml` then `*.yml`, each sorted — matches Python's
/// `sorted(dir.glob("*.yaml")) + sorted(dir.glob("*.yml"))`.
fn scenario_files(directory: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for ext in ["yaml", "yml"] {
        let pattern = directory.join(format!("*.{ext}"));
        if let Ok(paths) = glob(&pattern.to_string_lossy()) {
            let mut batch: Vec<PathBuf> = paths.filter_map(Result::ok).collect();
            batch.sort();
            out.extend(batch);
        }
    }
    out
}
