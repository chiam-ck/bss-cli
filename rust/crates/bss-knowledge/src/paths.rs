//! Doc allowlist for the knowledge indexer — the **doctrine source of truth**
//! for what the cockpit knowledge tool can cite. Port of
//! `packages/bss-knowledge/bss_knowledge/paths.py`.
//!
//! Adding a new path requires a doctrine review (a new DECISIONS.md entry);
//! adding `phases/V0_*.md` is a doctrine bug (grep guard 16). No glob — the
//! corpus is small enough that explicitness is cheap, and each file's `kind`
//! drives the search re-rank.

/// Repo-relative paths. The indexer joins each with the repo root. Keep the
/// per-runbook entries alphabetical (mirrors the Python tuple exactly).
pub const INDEXED_PATHS: &[&str] = &[
    "CLAUDE.md",
    "ARCHITECTURE.md",
    "DECISIONS.md",
    "TOOL_SURFACE.md",
    "ROADMAP.md",
    "CONTRIBUTING.md",
    "docs/HANDBOOK.md",
    // Per-runbook entries below — explicit, not a glob, so we can audit what
    // the LLM sees. Keep alphabetical.
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
    // phase-execution-runbook.md is INTENTIONALLY excluded — flagged stale in
    // the v0.19 doc survey; refreshing it is post-v0.20 work.
    "docs/runbooks/portal-auth.md",
    "docs/runbooks/post-login-self-serve-ops.md",
    "docs/runbooks/promo-codes.md",
    "docs/runbooks/snapshot-regeneration.md",
    "docs/runbooks/stripe-cutover.md",
    "docs/runbooks/three-provider-sandbox-soak.md",
    // phases/V0_*.md INTENTIONALLY NOT INDEXED — historical build plans mislead
    // the LLM. Doctrine guard 16 enforces.
];

/// Tag each indexed path with a `kind` for search filtering + re-rank.
/// Panics via `None` return only for a path outside the allowlist — callers
/// pass `INDEXED_PATHS` entries, so the mapping is total there.
pub fn kind_for(path: &str) -> Option<&'static str> {
    Some(match path {
        "CLAUDE.md" => "doctrine",
        "ARCHITECTURE.md" => "architecture",
        "DECISIONS.md" => "decisions",
        "TOOL_SURFACE.md" => "tool_surface",
        "ROADMAP.md" => "roadmap",
        "CONTRIBUTING.md" => "contributing",
        "docs/HANDBOOK.md" => "handbook",
        p if p.starts_with("docs/runbooks/") => "runbook",
        _ => return None,
    })
}

/// Tier-1 (hybrid) re-rank weights. Higher = preferred for the matching query
/// intent. `doctrine` beats `runbook` for "is this allowed?"; `handbook` beats
/// `decisions` for "how do I do X?". Unlisted kinds default to `1.0`.
///
/// The literals must parse to the same `f64` as the Python floats so the
/// weighted rank (`raw_rank * weight`) is bit-identical across the boundary.
pub fn kind_rank_weight(kind: &str) -> f64 {
    match kind {
        "doctrine" => 1.20,
        "handbook" => 1.10,
        "runbook" => 1.05,
        "architecture" => 1.00,
        "tool_surface" => 1.00,
        "decisions" => 0.90,
        "contributing" => 0.85,
        "roadmap" => 0.80,
        _ => 1.0,
    }
}
