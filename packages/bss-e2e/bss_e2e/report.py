"""Visual gallery generator for the v1.4 e2e suite.

The conftest's ``pytest_sessionfinish`` hook calls :func:`generate_index`
against the run-level report dir (``docs/e2e-reports/<UTC-ts>/``). The
result is a self-contained ``index.html`` linking each spec's
checkpoint screenshots, trace zip, and recorded video — no JS deps,
no asset CDN, opens in any browser.

Layout:

::

    docs/e2e-reports/20260525T173800Z/
    ├── index.html             ← what this module writes
    ├── signup-golden-path-smoke/
    │   ├── 01-authenticated.png
    │   ├── 02-signup-form-filled.png
    │   ├── 03-confirmation.png
    │   ├── 04-dashboard.png
    │   ├── trace.zip
    │   └── video.webm
    ├── public-promo-applied-at-signup/
    │   └── ...
    ...

The generator is intentionally template-free (small inline HTML +
inline CSS) so a future operator reading this file doesn't have to
chase a separate Jinja template.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fafafa;
  --fg: #1a1a1a;
  --muted: #6a6a6a;
  --border: #e4e4e4;
  --accent: #2563eb;
  --code-bg: #f0f0f0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e0e10;
    --fg: #e8e8e8;
    --muted: #9a9a9a;
    --border: #2a2a2a;
    --accent: #60a5fa;
    --code-bg: #1c1c1f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--bg); color: var(--fg);
  max-width: 1400px; margin: 0 auto;
}
h1 { font-size: 24px; margin: 0 0 4px 0; }
.run-meta { color: var(--muted); font-size: 14px; margin-bottom: 32px; }
.run-meta code { background: var(--code-bg); padding: 2px 6px; border-radius: 3px; font-size: 13px; }
.spec {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 24px;
  background: var(--bg);
}
.spec h2 { font-size: 18px; margin: 0 0 4px 0; color: var(--accent); }
.spec h2 a { color: inherit; text-decoration: none; }
.spec h2 a:hover { text-decoration: underline; }
.spec-artefacts {
  display: flex; gap: 12px; flex-wrap: wrap;
  margin: 12px 0; font-size: 13px; color: var(--muted);
}
.spec-artefacts a {
  color: var(--accent);
  text-decoration: none;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.spec-artefacts a:hover { background: var(--code-bg); }
.screenshots {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
  margin-top: 12px;
}
figure {
  margin: 0;
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
  background: var(--code-bg);
}
figure img {
  display: block;
  width: 100%;
  height: auto;
  cursor: zoom-in;
}
figcaption {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--muted);
  font-family: monospace;
  border-top: 1px solid var(--border);
}
video {
  display: block;
  max-width: 720px;
  width: 100%;
  margin: 12px 0;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: black;
}
details summary { cursor: pointer; padding: 8px 0; color: var(--muted); font-size: 13px; }
details summary:hover { color: var(--fg); }
.empty { color: var(--muted); font-style: italic; }

/* Summary hero + table */
.hero {
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--accent) 12%, var(--bg)),
    var(--bg));
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 24px;
  margin-bottom: 28px;
}
.hero-title { font-size: 14px; color: var(--muted); margin: 0 0 6px 0; }
.hero-headline { font-size: 28px; margin: 0 0 12px 0; font-weight: 600; }
.hero-meta {
  display: flex; gap: 24px; flex-wrap: wrap;
  font-size: 13px; color: var(--muted);
}
.hero-meta strong { color: var(--fg); font-weight: 600; }
.hero-pill {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
}
.hero-pill-pass { background: #16a34a; color: white; }
.hero-pill-fail { background: #dc2626; color: white; }
.hero-pill-mixed { background: #ca8a04; color: white; }
.summary-table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 28px;
  font-size: 13px;
}
.summary-table th, .summary-table td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.summary-table th {
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.summary-table td.duration { text-align: right; font-family: monospace; color: var(--muted); }
.summary-table td.status-cell { width: 80px; }
.summary-table tr:hover td { background: var(--code-bg); }
.summary-table a { color: var(--fg); text-decoration: none; }
.summary-table a:hover { color: var(--accent); text-decoration: underline; }
.status-passed { color: #16a34a; font-weight: 600; }
.status-failed { color: #dc2626; font-weight: 600; }
.status-skipped { color: #ca8a04; font-weight: 600; }
.status-error { color: #dc2626; font-weight: 600; }
"""


@dataclass(frozen=True)
class _SpecResult:
    """One row in the summary table."""

    spec_slug: str
    status: str  # "passed" | "failed" | "skipped" | "error"
    duration_s: float
    failure_message: str | None  # truncated, for inline display


def _classname_to_slug(classname: str, name: str) -> str:
    """junit's classname is ``tests.test_cockpit_browser`` and name is
    ``test_cockpit_propose_then_confirm`` — we want the spec_dir slug
    ``test-cockpit-propose-then-confirm`` (matches conftest._slugify)."""
    return _slugify(name)


def _parse_junit(junit_path: Path) -> tuple[dict[str, _SpecResult], dict[str, float | int]]:
    """Read junit.xml; return (results-by-slug, totals).

    Totals: ``passed`` / ``failed`` / ``skipped`` / ``errors`` / ``total`` /
    ``time_s`` (the suite's wall-clock duration).

    Best-effort: malformed / missing junit returns empty results so the
    gallery falls back to "no summary available" rather than crashing.
    """
    if not junit_path.is_file():
        return {}, {}
    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        return {}, {}
    root = tree.getroot()
    # pytest writes <testsuites><testsuite>...; some tools just emit
    # <testsuite> at the root. Handle both.
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        return {}, {}

    results: dict[str, _SpecResult] = {}
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for case in suite.findall("testcase"):
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        slug = _classname_to_slug(classname, name)
        try:
            duration = float(case.attrib.get("time", "0") or 0)
        except (TypeError, ValueError):
            duration = 0.0

        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")

        msg = None
        if failure is not None:
            status = "failed"
            msg = (failure.attrib.get("message") or "").splitlines()[0][:200] or None
        elif error is not None:
            status = "error"
            msg = (error.attrib.get("message") or "").splitlines()[0][:200] or None
        elif skipped is not None:
            status = "skipped"
            msg = (skipped.attrib.get("message") or "").splitlines()[0][:200] or None
        else:
            status = "passed"
        counts[status] += 1
        results[slug] = _SpecResult(
            spec_slug=slug,
            status=status,
            duration_s=duration,
            failure_message=msg,
        )

    totals: dict[str, float | int] = dict(counts)
    totals["total"] = sum(counts.values())
    try:
        totals["time_s"] = float(suite.attrib.get("time", "0") or 0)
    except (TypeError, ValueError):
        totals["time_s"] = 0.0
    return results, totals


def _slugify(s: str) -> str:
    """Filename-safe slug — kept in sync with conftest._slugify."""
    s = s.lower().replace("_", "-")
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "spec"


def _humanise_slug(slug: str) -> str:
    """``signup-golden-path-smoke`` → ``signup golden path smoke``."""
    return slug.replace("-", " ").replace("_", " ")


_STATUS_LABEL = {
    "passed": "✓ pass",
    "failed": "✗ fail",
    "skipped": "⚠ skip",
    "error": "✗ error",
}


def _hero(run_dir: Path, totals: dict[str, float | int]) -> str:
    """Top-of-page summary hero. When junit was missing/empty, falls back
    to a count of spec directories so the page still reads as a run."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")  # noqa: bss-clock
    total = int(totals.get("total") or 0)
    passed = int(totals.get("passed") or 0)
    failed = int(totals.get("failed") or 0)
    skipped = int(totals.get("skipped") or 0)
    errors = int(totals.get("error") or 0)
    time_s = float(totals.get("time_s") or 0.0)

    if total == 0:
        pill = '<span class="hero-pill hero-pill-mixed">no junit</span>'
        headline = "Run captured (test outcomes unknown)"
    elif failed + errors == 0 and skipped == 0:
        pill = f'<span class="hero-pill hero-pill-pass">all {passed} passed</span>'
        headline = f"All {passed} specs green"
    elif failed + errors == 0:
        pill = (
            f'<span class="hero-pill hero-pill-mixed">'
            f"{passed}/{total} passed · {skipped} skipped</span>"
        )
        headline = f"{passed} passed, {skipped} skipped"
    else:
        pill = (
            f'<span class="hero-pill hero-pill-fail">'
            f"{failed + errors} failed</span>"
        )
        headline = f"{passed} passed, {failed + errors} failed, {skipped} skipped"

    meta_lines = [
        f"Run id: <strong>{html.escape(run_dir.name)}</strong>",
        f"Generated: <strong>{html.escape(when)}</strong>",
    ]
    if time_s:
        meta_lines.append(f"Suite duration: <strong>{time_s:.1f} s</strong>")

    return (
        '<section class="hero">'
        f'<p class="hero-title">bss-cli e2e run {pill}</p>'
        f'<h1 class="hero-headline">{html.escape(headline)}</h1>'
        f'<div class="hero-meta">{" · ".join(meta_lines)}</div>'
        "</section>"
    )


def _summary_table(spec_dirs: list[Path], results: dict[str, _SpecResult]) -> str:
    """One-row-per-spec table at the top — quick scan + click-to-jump."""
    rows: list[str] = []
    for d in spec_dirs:
        slug = d.name
        human = _humanise_slug(slug)
        result = results.get(slug)
        if result is None:
            status_html = '<span class="status-skipped">— (no junit row)</span>'
            duration = "—"
        else:
            label = _STATUS_LABEL.get(result.status, result.status)
            status_html = (
                f'<span class="status-{result.status}">{html.escape(label)}</span>'
            )
            duration = f"{result.duration_s:.2f} s"
        rows.append(
            "<tr>"
            f'<td class="status-cell">{status_html}</td>'
            f'<td><a href="#{html.escape(slug)}">{html.escape(human)}</a></td>'
            f'<td class="duration">{html.escape(duration)}</td>'
            "</tr>"
        )
    if not rows:
        return ""
    return (
        '<table class="summary-table">'
        "<thead><tr><th>Status</th><th>Spec</th><th>Duration</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _spec_section(spec_dir: Path, result: _SpecResult | None = None) -> str:
    """Render a single spec's HTML block."""
    name = spec_dir.name
    human = _humanise_slug(name)
    screenshots = sorted(spec_dir.glob("*.png"))
    has_trace = (spec_dir / "trace.zip").is_file()
    has_video = (spec_dir / "video.webm").is_file()

    parts: list[str] = []
    parts.append('<section class="spec">')
    status_badge = ""
    if result is not None:
        label = _STATUS_LABEL.get(result.status, result.status)
        status_badge = (
            f' <span class="status-{result.status}">'
            f"({html.escape(label)} · {result.duration_s:.2f} s)</span>"
        )
    parts.append(
        f'<h2><a href="#{html.escape(name)}" id="{html.escape(name)}">'
        f"{html.escape(human)}</a>{status_badge}</h2>"
    )
    if result is not None and result.failure_message:
        parts.append(
            f'<p class="status-{result.status}" style="margin:4px 0 0 0;">'
            f"<code>{html.escape(result.failure_message)}</code></p>"
        )

    parts.append('<div class="spec-artefacts">')
    parts.append(f"<span>{len(screenshots)} screenshots</span>")
    if has_trace:
        parts.append(
            f'<a href="{html.escape(name)}/trace.zip" download>'
            "trace.zip (open with: <code>playwright show-trace</code>)</a>"
        )
    if has_video:
        parts.append(
            f'<a href="{html.escape(name)}/video.webm" download>video.webm</a>'
        )
    parts.append("</div>")

    if has_video:
        parts.append(
            "<details><summary>Watch the run (video)</summary>"
            f'<video controls preload="none" src="{html.escape(name)}/video.webm"></video>'
            "</details>"
        )

    if not screenshots:
        parts.append('<p class="empty">No screenshots captured for this spec.</p>')
    else:
        parts.append('<div class="screenshots">')
        for shot in screenshots:
            rel = f"{name}/{shot.name}"
            caption = shot.stem  # e.g. "01-authenticated"
            parts.append(
                f'<figure><a href="{html.escape(rel)}" target="_blank">'
                f'<img src="{html.escape(rel)}" alt="{html.escape(caption)}" loading="lazy">'
                f"</a>"
                f"<figcaption>{html.escape(caption)}</figcaption></figure>"
            )
        parts.append("</div>")

    parts.append("</section>")
    return "\n".join(parts)


def generate_index(run_dir: Path) -> Path:
    """Walk ``run_dir`` looking for per-spec subdirs and write
    ``run_dir/index.html``. Returns the path written.

    Each subdir under ``run_dir`` that contains at least one ``.png`` is
    treated as a spec section. Subdirs without screenshots still show up
    (so a fully-failing spec with no checkpoints isn't silently hidden).
    """
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run dir not found: {run_dir}")

    spec_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir())
    results, totals = _parse_junit(run_dir / "junit.xml")

    hero = _hero(run_dir, totals)
    summary = _summary_table(spec_dirs, results)
    sections = "\n".join(
        _spec_section(d, results.get(d.name)) for d in spec_dirs
    )

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>bss-cli e2e run — {html.escape(run_dir.name)}</title>
<style>{_CSS}</style>
</head>
<body>
{hero}
{summary}
{sections if sections else '<p class="empty">No specs found in this run directory.</p>'}
</body>
</html>
"""
    out = run_dir / "index.html"
    out.write_text(body, encoding="utf-8")
    return out
