"""Markdown → chunks. One chunk per ## or ### heading section.

Strategy:

* Top-level `## ` headings split chunks for runbooks + smaller docs.
* `### ` headings ALSO split chunks for the handbook + ARCHITECTURE
  (which use deep nesting; a single Part 8 chapter is too big to
  return as one snippet, so each `### N.N` lives on its own).
* DECISIONS.md uses a different shape — dated entries inside one big
  scrolling list. Chunk on `## YYYY-MM-DD` headings to preserve the
  decision-log entry boundary.
* CLAUDE.md uses `## ` for top-level sections (motto, scope, design
  rules, anti-patterns) which are exactly the right granularity.
* Frontmatter (YAML between `---`) is stripped from the first chunk.
* Code fences and table cells are kept intact within the chunk.

Anchor algorithm matches GitHub's: lowercase, spaces → hyphens, strip
non-alphanumeric except hyphens. Tested against actual GitHub renders.

Each chunk carries:
  source_path:  repo-relative
  anchor:       used in [link](#anchor) markdown — must match how
                Obsidian / GitHub resolve it
  heading_path: human-readable trail e.g. "Part 8 → 8.4 → Rotate"
  kind:         from paths.KIND_FOR_PATH
  content:      the section body, including the heading line
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Strip non-anchor chars. GitHub keeps letters, digits, hyphens, and
# underscores; turns spaces into hyphens; lowercases everything.
_ANCHOR_STRIP = re.compile(r"[^\w\- ]+", re.UNICODE)
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?\n)?---\s*\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    source_path: str
    anchor: str
    heading_path: str
    content: str


def _to_anchor(heading_text: str) -> str:
    """GitHub-flavoured anchor: lowercase, spaces→hyphens, strip non-word."""
    s = heading_text.strip().lower()
    # Drop emoji + any non-word/non-space/non-hyphen chars.
    s = _ANCHOR_STRIP.sub("", s)
    # Spaces and runs of whitespace → single hyphen.
    s = re.sub(r"\s+", "-", s)
    return s.strip("-")


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text)


def _heading_chunk_levels(source_path: str) -> set[int]:
    """Per-file split policy: which heading levels start a new chunk?"""
    if source_path in {"docs/HANDBOOK.md", "ARCHITECTURE.md"}:
        # Handbook + architecture nest deep; split on ## AND ###.
        return {2, 3}
    if source_path == "DECISIONS.md":
        # Dated entries are `## YYYY-MM-DD`. Split there only.
        return {2}
    # Default: ## only.
    return {2}


def chunk_markdown(source_path: str, text: str) -> list[Chunk]:
    """Split a markdown doc into chunks. Returns at least one chunk
    (the preamble) when there are no matching headings."""
    text = _strip_frontmatter(text)
    levels = _heading_chunk_levels(source_path)

    # Walk lines, tracking the deepest heading at each level so we can
    # build the heading_path trail. A new chunk starts when we hit a
    # heading at one of the configured `levels`.
    lines = text.splitlines(keepends=True)
    chunks: list[Chunk] = []
    current_lines: list[str] = []
    # index: heading level → text. Reset deeper levels on shallow heading.
    heading_stack: dict[int, str] = {}
    current_heading: str | None = None
    current_level: int | None = None

    def flush() -> None:
        nonlocal current_lines, current_heading, current_level
        body = "".join(current_lines).rstrip()
        if not body:
            return
        if current_heading is None:
            # Preamble before any heading. Anchor + path are best-effort.
            anchor = _to_anchor(source_path.replace("/", "-"))
            heading_path = source_path
        else:
            anchor = _to_anchor(current_heading)
            # heading_path trail: levels < current, joined by " → ".
            assert current_level is not None
            trail = [
                heading_stack[lvl]
                for lvl in sorted(heading_stack.keys())
                if lvl < current_level
            ]
            trail.append(current_heading)
            heading_path = " → ".join(trail)
        chunks.append(
            Chunk(
                source_path=source_path,
                anchor=anchor,
                heading_path=heading_path,
                content=body,
            )
        )

    for line in lines:
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            heading_text = m.group(2)
            # Update heading stack: drop deeper levels, set this level.
            for deeper in [lv for lv in heading_stack if lv >= level]:
                del heading_stack[deeper]
            heading_stack[level] = heading_text
            if level in levels:
                # Start a new chunk at this heading.
                flush()
                current_lines = [line]
                current_heading = heading_text
                current_level = level
                continue
        current_lines.append(line)

    flush()
    return chunks
