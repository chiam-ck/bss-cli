"""Seed inventory reference data.

- 1000 MSISDNs (90000000–90000999), all 'available'
- 1000 eSIM profiles with generated ICCIDs, IMSIs, stubbed ki_refs

# NEVER store actual Ki values. ki_ref is a reference to a hypothetical HSM slot.
# Real Ki storage is HSM territory and explicitly out of scope for BSS-CLI.
"""

import secrets
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SMDP_SERVER = "smdp.bss-cli.local"


async def seed(session: AsyncSession) -> None:
    # ── MSISDN Pool ──────────────────────────────────────────────────
    # 1000 numbers: 90000000–90000999
    for i in range(1000):
        msisdn = f"9000{i:04d}"
        await session.execute(text("""
            INSERT INTO inventory.msisdn_pool (msisdn, status)
            VALUES (:msisdn, 'available')
            ON CONFLICT (msisdn) DO NOTHING
        """), {"msisdn": msisdn})

    # ── eSIM Profile Pool ────────────────────────────────────────────
    # NEVER store actual Ki values. ki_ref is a reference to a hypothetical HSM slot.
    # Real Ki storage is HSM territory and explicitly out of scope for BSS-CLI.
    for i in range(1000):
        iccid = f"8910101{str(i).zfill(12)}"
        imsi = f"525010000{str(i).zfill(6)}"
        ki_ref = f"hsm://ref/{uuid4()}"
        matching_id = secrets.token_hex(8)  # 16 hex chars
        activation_code = f"LPA:1${SMDP_SERVER}${matching_id}"

        await session.execute(text("""
            INSERT INTO inventory.esim_profile
                (iccid, imsi, ki_ref, profile_state, smdp_server, matching_id, activation_code)
            VALUES (:iccid, :imsi, :ki_ref, 'available', :smdp_server, :matching_id, :activation_code)
            ON CONFLICT (iccid) DO NOTHING
        """), {
            "iccid": iccid,
            "imsi": imsi,
            "ki_ref": ki_ref,
            "smdp_server": SMDP_SERVER,
            "matching_id": matching_id,
            "activation_code": activation_code,
        })
