"""Seed catalog reference data.

- 1 product specification
- 3 product offerings (PLAN_S, PLAN_M, PLAN_L) with prices and allowances
- 3 VAS offerings
- 3 service specifications (1 CFS + 2 RFS)
- 3 product-to-service mappings
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def seed(session: AsyncSession) -> None:
    # ── Product Specification ────────────────────────────────────────
    await session.execute(text("""
        INSERT INTO catalog.product_specification (id, name, description, brand, lifecycle_status)
        VALUES ('SPEC_MOBILE_PREPAID', 'Mobile Prepaid Bundle', 'Bundled prepaid mobile plan', 'BSS-CLI', 'active')
        ON CONFLICT (id) DO NOTHING
    """))

    # ── Product Offerings ────────────────────────────────────────────
    # valid_from / valid_to are NULL → "always available". v0.7 introduces
    # time-bound rows; the seeded plans are not bound by default.
    offerings = [
        ("PLAN_S", "Lite", "SPEC_MOBILE_PREPAID", True, True, "active"),
        ("PLAN_M", "Standard", "SPEC_MOBILE_PREPAID", True, True, "active"),
        ("PLAN_L", "Max", "SPEC_MOBILE_PREPAID", True, True, "active"),
    ]
    for oid, name, spec, is_bundle, is_sellable, status in offerings:
        await session.execute(text("""
            INSERT INTO catalog.product_offering
                (id, name, spec_id, is_bundle, is_sellable, lifecycle_status, valid_from, valid_to)
            VALUES (:id, :name, :spec_id, :is_bundle, :is_sellable, :status, NULL, NULL)
            ON CONFLICT (id) DO NOTHING
        """), {"id": oid, "name": name, "spec_id": spec, "is_bundle": is_bundle,
               "is_sellable": is_sellable, "status": status})

    # ── Product Offering Prices ──────────────────────────────────────
    # valid_from / valid_to are NULL → "always available". Promo prices
    # land via `bss admin catalog set-price --valid-from ... --valid-to ...`.
    prices = [
        ("PRICE_PLAN_S", "PLAN_S", "recurring", 1, "month", 10.00, "SGD"),
        ("PRICE_PLAN_M", "PLAN_M", "recurring", 1, "month", 25.00, "SGD"),
        ("PRICE_PLAN_L", "PLAN_L", "recurring", 1, "month", 45.00, "SGD"),
    ]
    for pid, oid, ptype, length, period, amount, currency in prices:
        await session.execute(text("""
            INSERT INTO catalog.product_offering_price
                (id, offering_id, price_type, recurring_period_length,
                 recurring_period_type, amount, currency, valid_from, valid_to)
            VALUES (:id, :offering_id, :price_type, :length, :period,
                    :amount, :currency, NULL, NULL)
            ON CONFLICT (id) DO NOTHING
        """), {"id": pid, "offering_id": oid, "price_type": ptype, "length": length,
               "period": period, "amount": amount, "currency": currency})

    # ── Bundle Allowances ────────────────────────────────────────────
    # quantity in base units: data=MB, voice=minutes, SMS=count. -1 = unlimited.
    # data_roaming (v0.17) is additive: an exhausted roaming bucket
    # blocks roaming usage but does NOT block the subscription itself.
    allowances = [
        # PLAN_S: 5GB data, 100min voice, 100 SMS, no roaming included
        ("BA_S_DATA", "PLAN_S", "data", 5120, "mb"),
        ("BA_S_VOICE", "PLAN_S", "voice", 100, "minutes"),
        ("BA_S_SMS", "PLAN_S", "sms", 100, "count"),
        ("BA_S_ROAM", "PLAN_S", "data_roaming", 0, "mb"),
        # PLAN_M: 30GB data, unlimited voice/SMS, 500MB roaming
        ("BA_M_DATA", "PLAN_M", "data", 30720, "mb"),
        ("BA_M_VOICE", "PLAN_M", "voice", -1, "minutes"),
        ("BA_M_SMS", "PLAN_M", "sms", -1, "count"),
        ("BA_M_ROAM", "PLAN_M", "data_roaming", 500, "mb"),
        # PLAN_L: 150GB data, unlimited voice/SMS, 2GB roaming
        ("BA_L_DATA", "PLAN_L", "data", 153600, "mb"),
        ("BA_L_VOICE", "PLAN_L", "voice", -1, "minutes"),
        ("BA_L_SMS", "PLAN_L", "sms", -1, "count"),
        ("BA_L_ROAM", "PLAN_L", "data_roaming", 2048, "mb"),
    ]
    for aid, oid, atype, qty, unit in allowances:
        await session.execute(text("""
            INSERT INTO catalog.bundle_allowance (id, offering_id, allowance_type, quantity, unit)
            VALUES (:id, :offering_id, :atype, :qty, :unit)
            ON CONFLICT (id) DO NOTHING
        """), {"id": aid, "offering_id": oid, "atype": atype, "qty": qty, "unit": unit})

    # ── VAS Offerings ────────────────────────────────────────────────
    vas = [
        ("VAS_DATA_1GB", "Data Top-Up 1GB", 3.00, "SGD", "data", 1024, "mb", None),
        ("VAS_DATA_5GB", "Data Top-Up 5GB", 12.00, "SGD", "data", 5120, "mb", None),
        ("VAS_UNLIMITED_DAY", "Unlimited Data Day Pass", 5.00, "SGD", "data", -1, "mb", 24),
        ("VAS_ROAMING_1GB", "Roaming Data 1GB", 8.00, "SGD", "data_roaming", 1024, "mb", None),
    ]
    for vid, name, price, cur, atype, qty, unit, expiry in vas:
        await session.execute(text("""
            INSERT INTO catalog.vas_offering
                (id, name, price_amount, currency, allowance_type, allowance_quantity, allowance_unit, expiry_hours)
            VALUES (:id, :name, :price, :currency, :atype, :qty, :unit, :expiry)
            ON CONFLICT (id) DO NOTHING
        """), {"id": vid, "name": name, "price": price, "currency": cur,
               "atype": atype, "qty": qty, "unit": unit, "expiry": expiry})

    # ── Service Specifications ───────────────────────────────────────
    specs = [
        ("SSPEC_CFS_MOBILE_BROADBAND", "Mobile Broadband CFS", "CFS", {}),
        ("SSPEC_RFS_DATA_BEARER", "Data Bearer RFS", "RFS", {}),
        ("SSPEC_RFS_VOICE_BEARER", "Voice Bearer RFS", "RFS", {}),
    ]
    for sid, name, stype, params in specs:
        await session.execute(text("""
            INSERT INTO catalog.service_specification (id, name, type, parameters)
            VALUES (:id, :name, :type, CAST(:params AS jsonb))
            ON CONFLICT (id) DO NOTHING
        """), {"id": sid, "name": name, "type": stype, "params": "{}"})

    # ── Product-to-Service Mappings ──────────────────────────────────
    # All three plans map to the same CFS + both RFS (v0.1 simplification)
    for oid in ["PLAN_S", "PLAN_M", "PLAN_L"]:
        await session.execute(text("""
            INSERT INTO catalog.product_to_service_mapping (offering_id, cfs_spec_id, rfs_spec_ids)
            SELECT :offering_id, :cfs_spec_id,
                   ARRAY['SSPEC_RFS_DATA_BEARER', 'SSPEC_RFS_VOICE_BEARER']
            WHERE NOT EXISTS (
                SELECT 1 FROM catalog.product_to_service_mapping
                WHERE offering_id = :offering_id
            )
        """), {"offering_id": oid, "cfs_spec_id": "SSPEC_CFS_MOBILE_BROADBAND"})
