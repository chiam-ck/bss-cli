"""v1.1.1 — chat sees the current (effective) charge + an active-discount note.

_annotate_pricing shapes the subscription dict the customer chat reads so the
LLM reports what's actually paid, not the base list price.
"""

from bss_orchestrator.tools.mine_wrappers import _annotate_pricing


def _sub(**kw):
    base = {"priceAmount": "25.00", "priceCurrency": "SGD", "effectiveAmount": "25.00",
            "discountType": None, "discountValue": None, "discountPeriodsRemaining": 0}
    base.update(kw)
    return base


def test_no_discount_charge_is_base():
    s = _annotate_pricing(_sub())
    assert s["currentMonthlyCharge"] == "SGD 25.00"
    assert s["activeDiscount"] is None


def test_active_multi_period_discount():
    s = _annotate_pricing(_sub(
        effectiveAmount="20.00", discountType="percent", discountValue="20.00",
        discountPeriodsRemaining=2,
    ))
    assert s["currentMonthlyCharge"] == "SGD 20.00"  # what they pay now
    assert "20% off" in s["activeDiscount"]
    assert "2 more renewals at this price" in s["activeDiscount"]
    assert "then SGD 25.00/mo" in s["activeDiscount"]


def test_single_use_one_renewal_singular():
    s = _annotate_pricing(_sub(
        effectiveAmount="20.00", discountType="percent", discountValue="20.00",
        discountPeriodsRemaining=1,
    ))
    assert "1 more renewal at this price" in s["activeDiscount"]  # no plural 's'


def test_perpetual_discount():
    s = _annotate_pricing(_sub(
        effectiveAmount="20.00", discountType="percent", discountValue="20.00",
        discountPeriodsRemaining=-1,
    ))
    assert s["activeDiscount"] == "20% off (ongoing)"


def test_exhausted_discount_shows_no_note():
    # discount fields present but remaining 0 → reverted to full price, no note
    s = _annotate_pricing(_sub(
        effectiveAmount="25.00", discountType="percent", discountValue="20.00",
        discountPeriodsRemaining=0,
    ))
    assert s["currentMonthlyCharge"] == "SGD 25.00"
    assert s["activeDiscount"] is None


def test_absolute_discount_label():
    s = _annotate_pricing(_sub(
        effectiveAmount="20.00", discountType="absolute", discountValue="5.00",
        discountPeriodsRemaining=3,
    ))
    assert "SGD 5.00 off" in s["activeDiscount"]
