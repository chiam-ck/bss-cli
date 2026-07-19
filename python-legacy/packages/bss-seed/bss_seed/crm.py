"""Seed CRM reference data.

- 5 agents (Alice, Bob, Carol, Dave, System)
- 12 SLA policies (4 priorities x 3 ticket types)
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def seed(session: AsyncSession) -> None:
    # ── Agents ───────────────────────────────────────────────────────
    agents = [
        ("AGT-001", "Alice Tan", "alice.tan@bss-cli.local", "csr", "active"),
        ("AGT-002", "Bob Lim", "bob.lim@bss-cli.local", "csr", "active"),
        ("AGT-003", "Carol Ng", "carol.ng@bss-cli.local", "supervisor", "active"),
        ("AGT-004", "Dave Koh", "dave.koh@bss-cli.local", "engineer", "active"),
        ("AGT-SYS", "System", "system@bss-cli.local", "system", "active"),
    ]
    for aid, name, email, role, status in agents:
        await session.execute(text("""
            INSERT INTO crm.agent (id, name, email, role, status)
            VALUES (:id, :name, :email, :role, :status)
            ON CONFLICT (id) DO NOTHING
        """), {"id": aid, "name": name, "email": email, "role": role, "status": status})

    # ── SLA Policies ─────────────────────────────────────────────────
    # 4 priorities x 3 ticket types = 12 policies
    # Target resolution times in minutes per PHASE_02.md spec
    sla_data = {
        "billing_dispute": {"low": 2880, "normal": 1440, "high": 480, "urgent": 120},
        "service_outage": {"low": 480, "normal": 240, "high": 60, "urgent": 30},
        "configuration": {"low": 2880, "normal": 1440, "high": 720, "urgent": 240},
    }
    for ticket_type, priorities in sla_data.items():
        for priority, minutes in priorities.items():
            sla_id = f"SLA_{ticket_type}_{priority}".upper()
            await session.execute(text("""
                INSERT INTO crm.sla_policy (id, ticket_type, priority, target_resolution_minutes)
                VALUES (:id, :ticket_type, :priority, :minutes)
                ON CONFLICT (id) DO NOTHING
            """), {"id": sla_id, "ticket_type": ticket_type, "priority": priority, "minutes": minutes})
