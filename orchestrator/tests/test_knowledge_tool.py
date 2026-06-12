"""Knowledge tool registration + citation-guard regex tests."""

from __future__ import annotations

from bss_orchestrator.tools import TOOL_PROFILES, TOOL_REGISTRY


class TestProfileMembership:
    def test_search_in_operator_cockpit(self):
        assert "knowledge.search" in TOOL_PROFILES["operator_cockpit"]

    def test_get_in_operator_cockpit(self):
        assert "knowledge.get" in TOOL_PROFILES["operator_cockpit"]

    def test_search_NOT_in_customer_self_serve(self):
        """Doctrine: customer chat must not get RAG over operator runbooks."""
        assert "knowledge.search" not in TOOL_PROFILES["customer_self_serve"]

    def test_get_NOT_in_customer_self_serve(self):
        assert "knowledge.get" not in TOOL_PROFILES["customer_self_serve"]

    def test_both_registered(self):
        assert "knowledge.search" in TOOL_REGISTRY
        assert "knowledge.get" in TOOL_REGISTRY


class TestSearchToolDescription:
    """The tool description carries the citation rule verbatim — the
    LLM sees this on every tool-call decision. Regression-protect it."""

    def test_search_doc_mentions_anchor(self):
        from bss_orchestrator.tools.knowledge import knowledge_search
        doc = (knowledge_search.__doc__ or "").lower()
        assert "anchor" in doc
        assert "cite" in doc

    def test_search_doc_mentions_no_paraphrase_rule(self):
        from bss_orchestrator.tools.knowledge import knowledge_search
        doc = (knowledge_search.__doc__ or "").lower()
        # The rule is "do not paraphrase without citing" in many forms.
        assert "paraphrase" in doc or "training data" in doc


class TestCitationGuardRegex:
    """Mirror tests for the REPL + cockpit-route _RE_KNOWLEDGE_CLAIM
    regex. Both surfaces use the same pattern; we test the REPL's
    copy and trust the cockpit's mirror is identical.

    The guard is conservative — it catches the most common
    false-confident phrasings. Phrases NOT matched are NOT bugs;
    the search index is the primary defence."""

    def _matches(self, text: str) -> bool:
        # Import lazily so we don't pay the REPL's heavy imports for
        # every test in this file.
        from cli.bss_cli.repl import _claims_handbook  # type: ignore[import-not-found]
        return _claims_handbook(text)

    def test_per_handbook(self):
        assert self._matches("Per the handbook, you should rotate every 90d.")

    def test_according_to_doctrine(self):
        assert self._matches("According to doctrine, this is forbidden.")

    def test_handbook_says(self):
        assert self._matches("The handbook says to use bss-clock.")

    def test_per_claude_md(self):
        assert self._matches("Per CLAUDE.md, avoid raw datetime.utcnow().")

    def test_neutral_text_does_not_match(self):
        assert not self._matches("Done. Customer terminated.")

    def test_handbook_word_alone_does_not_match(self):
        """Just mentioning "handbook" without claim verb is NOT a trip."""
        assert not self._matches("I read the handbook earlier.")

    def test_empty_text_does_not_match(self):
        assert not self._matches("")
