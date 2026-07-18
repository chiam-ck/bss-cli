//! Cross-crate cockpit-guard invariant. Relocated from `bss_cockpit::guards`
//! (P7 s18a) because it needs `bss_orchestrator`, which `bss-cockpit` can't depend
//! on (the dependency direction is orchestrator → cockpit). The CSR portal depends
//! on both, so the check lives here.

use bss_cockpit::is_destructive;
use bss_orchestrator::safety::DESTRUCTIVE_TOOLS;
use bss_orchestrator::tools::profile_tools;

/// The cockpit's destructive list is NOT safety's, and the difference is
/// deliberate. This pins the direction that would actually hurt: a tool the loop
/// BLOCKS but the cockpit can't stage for `/confirm` would strand the operator with
/// no way to authorise it. That set must stay empty.
#[test]
fn no_cockpit_tool_is_blocked_without_being_stageable() {
    let profile: Vec<&str> = profile_tools("operator_cockpit").to_vec();
    let stranded: Vec<&&str> = DESTRUCTIVE_TOOLS
        .iter()
        .filter(|t| profile.contains(t) && !is_destructive(t))
        .collect();
    assert!(
        stranded.is_empty(),
        "these cockpit tools are blocked by safety but cannot be staged for \
         /confirm — the operator would hit a wall with no way through: {stranded:?}"
    );
}
