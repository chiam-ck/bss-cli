"""Pre-baked KYC attestation constants for the signup chain.

v0.4 / v0.8 used this module for natural-language prompt templates the
orchestrator agent consumed. v0.11 retired the orchestrator-mediated
signup chain in favour of a deterministic direct-API path
(``routes/signup.py``); the prompt template is gone with it.

What remains here are the pre-baked KYC attestation constants the
direct-write chain still passes to ``crm.attest_kyc`` when the
portal is running with ``BSS_PORTAL_KYC_PROVIDER=prebaked`` (dev
default; production runs ``didit``):

* ``KYC_PREBAKED_ATTESTATION_ID`` — the displayed prebaked attestation
  id ("KYC-PREBAKED-001"). Stable across all signups so the form copy
  reads consistently.
* ``KYC_PREBAKED_SIGNATURE_TEMPLATE`` — per-customer signature template.
  The actual signature passed to CRM is derived by formatting in the
  customer's email so the
  ``customer.attest_kyc.document_hash_unique_per_tenant`` policy
  doesn't reject a duplicate document hash. See DECISIONS.md
  2026-04-23 ("KYC attestation uses per-customer signatures") for
  rationale.

v0.19 — module + constant names changed from "Myinfo" to "prebaked"
to match the v0.15 KYC-adapter doctrine. The provider value the
adapter returns is ``"prebaked"`` (real production value is
``"didit"``); v0.4's "myinfo" string was a doctrine-stale leftover.
"""

from __future__ import annotations

KYC_PREBAKED_ATTESTATION_ID = "KYC-PREBAKED-001"
KYC_PREBAKED_SIGNATURE_TEMPLATE = "prebaked-simulated-v1::{email}"
