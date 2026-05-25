# e2e reports

This directory holds the per-run artefacts from `make e2e` (v1.4+). The dir
itself is gitignored except for this README — actual reports are local-only.

## Layout

Each invocation of `make e2e` creates a timestamped subdirectory:

```
docs/e2e-reports/
└── 20260525T133045Z/
    ├── report.html        # pytest-html — self-contained, openable in any browser
    ├── junit.xml          # JUnit XML — for diffing across runs or CI ingestion later
    └── traces/            # Playwright trace zips, one per failing spec (when wired in)
```

Timestamp format: `YYYYMMDDTHHMMSSZ` (UTC, ISO 8601 basic). Lexicographic sort
== chronological sort.

## Why not git-track?

- **Size** — HTML reports + traces grow fast; a green run is ~200 KB, a failing
  trace adds 1–5 MB per spec. Repo bloat compounds across the project.
- **Volatility** — every run produces a new directory. Diffs would be pure
  noise.
- **Local-truth** — the report is a snapshot of *your* dev box's stack at
  *that* moment. CI artefacts (v1.4.1+) will live in GH Actions, not git.

## Pruning

Old runs are safe to delete — no other tooling references them by path. A
sensible cleanup keeps the last 5 runs:

```bash
ls -1dt docs/e2e-reports/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf
```

## See also

- `phases/V1_4_0.md` — phase doc for the suite design.
- `docker-compose.e2e.yml` — provider overrides applied during a run.
- `packages/bss-e2e/` — the suite itself.
