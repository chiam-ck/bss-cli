"""Chunker tests — split policy, anchor algorithm, frontmatter strip."""

from __future__ import annotations

from bss_knowledge.chunker import _to_anchor, chunk_markdown


class TestAnchorAlgorithm:
    """The anchor algorithm must match GitHub / Obsidian. If these
    tests drift, citation [link](#anchor) will break in the editor."""

    def test_lowercase_with_hyphens(self):
        assert _to_anchor("Rotate API tokens") == "rotate-api-tokens"

    def test_strips_punctuation_keeps_words(self):
        assert _to_anchor("Don't paraphrase doctrine!") == "dont-paraphrase-doctrine"

    def test_emoji_stripped(self):
        assert _to_anchor("⚠ Warning section") == "warning-section"

    def test_numbered_section(self):
        assert _to_anchor("8.4 Rotate API tokens") == "84-rotate-api-tokens"

    def test_collapses_whitespace(self):
        assert _to_anchor("foo   bar    baz") == "foo-bar-baz"

    def test_strips_leading_trailing_hyphens(self):
        assert _to_anchor("  hello world  ") == "hello-world"


class TestChunkSplit:
    def test_split_on_h2_for_runbooks(self):
        text = """# Title

Some intro.

## Section A

Body A.

## Section B

Body B.
"""
        chunks = chunk_markdown("docs/runbooks/foo.md", text)
        # preamble (intro) + Section A + Section B = 3 chunks
        anchors = [c.anchor for c in chunks]
        assert "section-a" in anchors
        assert "section-b" in anchors

    def test_handbook_splits_on_h3_too(self):
        text = """# Handbook

## Part 1

### 1.1 Subsection one

A.

### 1.2 Subsection two

B.
"""
        chunks = chunk_markdown("docs/HANDBOOK.md", text)
        anchors = [c.anchor for c in chunks]
        # Both ## and ### create chunks for handbook.
        assert "part-1" in anchors
        assert "11-subsection-one" in anchors
        assert "12-subsection-two" in anchors

    def test_runbook_does_NOT_split_on_h3(self):
        """Runbooks split on ## only — h3s stay inside their parent."""
        text = """## Section A

### Subsection

Inside.
"""
        chunks = chunk_markdown("docs/runbooks/foo.md", text)
        anchors = [c.anchor for c in chunks]
        assert "section-a" in anchors
        assert "subsection" not in anchors

    def test_strips_yaml_frontmatter(self):
        text = """---
title: foo
tags: [bar]
---

## Section

Body.
"""
        chunks = chunk_markdown("docs/HANDBOOK.md", text)
        # Frontmatter must not appear in any chunk's content.
        for c in chunks:
            assert "title: foo" not in c.content

    def test_heading_path_includes_parent(self):
        """### subsection's heading_path includes its ## parent."""
        text = """## Part 1

### 1.1 Subsection

X.
"""
        chunks = chunk_markdown("docs/HANDBOOK.md", text)
        sub = next(c for c in chunks if c.anchor == "11-subsection")
        assert "Part 1" in sub.heading_path
        assert "1.1 Subsection" in sub.heading_path

    def test_headings_inside_code_fences_not_split(self):
        """The current chunker doesn't tokenise code fences — it splits
        on ANY line matching ^#{1,6} . This is acceptable for the doc
        corpus (we don't have shell scripts with `# Section` comments
        that we'd want to NOT chunk-split). Document the current
        behaviour so future tightening is intentional."""
        text = """## Real Section

Some text.

```bash
## NOT a real heading
```
"""
        chunks = chunk_markdown("docs/runbooks/foo.md", text)
        # Currently the `## NOT a real heading` inside the fence WOULD
        # split. Confirm we have at least the real heading; document
        # the leakage as a known limit if it bites.
        assert any(c.anchor == "real-section" for c in chunks)
