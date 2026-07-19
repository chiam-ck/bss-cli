"""Select the active KYC adapter from env config (v0.15)."""

from __future__ import annotations

from .adapter import KycVerificationAdapter
from .didit import DiditConfig, DiditKycAdapter
from .prebaked import PrebakedKycAdapter


def select_kyc_adapter(
    *,
    name: str,
    didit_api_key: str = "",
    didit_workflow_id: str = "",
    session_factory=None,
) -> KycVerificationAdapter:
    """Resolve ``BSS_PORTAL_KYC_PROVIDER`` to a concrete adapter.

    Fail-fast on unknown name or missing credentials — silent downgrade
    to a different adapter is a doctrine violation.
    """
    if name == "prebaked":
        return PrebakedKycAdapter()
    if name == "didit":
        if not didit_api_key:
            raise RuntimeError(
                "BSS_PORTAL_KYC_PROVIDER=didit requires "
                "BSS_PORTAL_KYC_DIDIT_API_KEY"
            )
        if not didit_workflow_id:
            raise RuntimeError(
                "BSS_PORTAL_KYC_PROVIDER=didit requires "
                "BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID"
            )
        if session_factory is None:
            raise RuntimeError(
                "DiditKycAdapter requires a DB session factory for "
                "corroboration lookup"
            )
        return DiditKycAdapter(
            config=DiditConfig(
                api_key=didit_api_key,
                workflow_id=didit_workflow_id,
            ),
            session_factory=session_factory,
        )
    raise RuntimeError(
        f"Unknown BSS_PORTAL_KYC_PROVIDER={name!r}; expected "
        "'prebaked' | 'didit'"
    )
