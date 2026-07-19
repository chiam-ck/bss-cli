"""Seed provisioning fault injection rules.

- 6 rules covering HLR_PROVISION, PCRF_POLICY_PUSH, OCS_BALANCE_INIT,
  ESIM_PROFILE_PREPARE, HLR_DEPROVISION — all disabled by default.

Seeded so scenarios can enable them by ID without having to create them.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def seed(session: AsyncSession) -> None:
    rules = [
        ("FI_HLR_PROV_FAIL", "HLR_PROVISION", "fail_first_attempt", 0.30),
        ("FI_HLR_PROV_STUCK", "HLR_PROVISION", "stuck", 0.05),
        ("FI_PCRF_SLOW", "PCRF_POLICY_PUSH", "slow", 0.20),
        ("FI_OCS_FAIL", "OCS_BALANCE_INIT", "fail_first_attempt", 0.10),
        ("FI_ESIM_FAIL", "ESIM_PROFILE_PREPARE", "fail_first_attempt", 0.15),
        ("FI_HLR_DEPROV_STUCK", "HLR_DEPROVISION", "stuck", 0.05),
    ]
    for fid, task_type, fault_type, probability in rules:
        await session.execute(text("""
            INSERT INTO provisioning.fault_injection (id, task_type, fault_type, probability, enabled)
            VALUES (:id, :task_type, :fault_type, :probability, false)
            ON CONFLICT (id) DO NOTHING
        """), {"id": fid, "task_type": task_type, "fault_type": fault_type, "probability": probability})
