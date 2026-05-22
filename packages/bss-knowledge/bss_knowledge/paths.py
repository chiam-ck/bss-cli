"""Doc allowlist for the knowledge indexer.

This is the **doctrine source of truth** for what the cockpit knowledge
tool can cite. Adding a new path requires a doctrine review (a new
DECISIONS.md entry); adding `phases/V0_*.md` is a doctrine bug
(grep guard 16 in `make doctrine-check`).

Why no glob:

* Globs invite drift. Someone adds `docs/internal/*.md` and ships it
  to the LLM without anyone noticing.
* The corpus is small enough (sub-1MB) that explicitness is cheap.
* Each file's `kind` is meaningful for the search ranking — globs
  obscure that.

`KIND_FOR_PATH` maps each indexed path to a `kind` string used by:

* The `kinds=[...]` filter on `knowledge.search` (e.g. operator scopes
  to `["doctrine", "handbook"]` for an "is this allowed?" question).
* Re-ranking (Tier-1 hybrid): doctrine outranks runbooks for
  prohibition queries; handbook outranks decisions for how-to queries.
"""

from __future__ import annotations

# Repo-relative paths. Indexer joins with the repo root.
INDEXED_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "ARCHITECTURE.md",
    "DECISIONS.md",
    "TOOL_SURFACE.md",
    "ROADMAP.md",
    "CONTRIBUTING.md",
    "docs/HANDBOOK.md",
    # Per-runbook entries below — explicit, not a glob, so we can audit
    # what the LLM sees. Keep alphabetical.
    "docs/runbooks/add-product-offering.md",
    "docs/runbooks/adding-tool-to-customer-self-serve.md",
    "docs/runbooks/api-token-rotation.md",
    "docs/runbooks/chat-cap-tripped.md",
    "docs/runbooks/chat-escalated-case.md",
    "docs/runbooks/chat-ownership-trip.md",
    "docs/runbooks/chat-transcript-retention.md",
    "docs/runbooks/cny-promo.md",
    "docs/runbooks/cockpit.md",
    "docs/runbooks/jaeger-byoi.md",
    "docs/runbooks/migrating-customers-to-new-price.md",
    "docs/runbooks/mnp-port-flows.md",
    "docs/runbooks/payment-idempotency.md",
    # phase-execution-runbook.md is INTENTIONALLY excluded — flagged stale
    # in the v0.19 doc survey; refreshing it is post-v0.20 work.
    "docs/runbooks/portal-auth.md",
    "docs/runbooks/post-login-self-serve-ops.md",
    "docs/runbooks/promo-codes.md",
    "docs/runbooks/snapshot-regeneration.md",
    "docs/runbooks/stripe-cutover.md",
    "docs/runbooks/three-provider-sandbox-soak.md",
    # phases/V0_*.md INTENTIONALLY NOT INDEXED — historical build plans
    # mislead the LLM. Doctrine guard 16 enforces.
)


def _kind(path: str) -> str:
    """Tag each indexed path with a `kind` for search filtering."""
    if path == "CLAUDE.md":
        return "doctrine"
    if path == "ARCHITECTURE.md":
        return "architecture"
    if path == "DECISIONS.md":
        return "decisions"
    if path == "TOOL_SURFACE.md":
        return "tool_surface"
    if path == "ROADMAP.md":
        return "roadmap"
    if path == "CONTRIBUTING.md":
        return "contributing"
    if path == "docs/HANDBOOK.md":
        return "handbook"
    if path.startswith("docs/runbooks/"):
        return "runbook"
    raise ValueError(f"no kind mapping for indexed path: {path}")


KIND_FOR_PATH: dict[str, str] = {p: _kind(p) for p in INDEXED_PATHS}

# Tier-1 (hybrid) re-rank weights. Higher = preferred for the matching
# query intent. `doctrine` beats `runbook` for "is this allowed?";
# `handbook` beats `decisions` for "how do I do X?". Tier-0 ignores.
KIND_RANK_WEIGHTS: dict[str, float] = {
    "doctrine": 1.20,
    "handbook": 1.10,
    "runbook": 1.05,
    "architecture": 1.00,
    "tool_surface": 1.00,
    "decisions": 0.90,
    "contributing": 0.85,
    "roadmap": 0.80,
}
