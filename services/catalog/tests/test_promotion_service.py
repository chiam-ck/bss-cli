"""v1.1 — PromotionService.create_promotion two-system saga.

Runs against the live dev DB (like the other catalog write tests): unique
promotion ids + finally-cleanup. The LoyaltyClient is faked so the saga
logic is tested without touching the real loyalty-cli (no entitlement-ledger
pollution) — the LoyaltyClient's own contract is covered in bss-clients.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from bss_clients import NotFound, PolicyViolationFromServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bss_catalog.policies import PolicyViolation
from bss_catalog.promotion_repository import PromotionRepository
from bss_catalog.promotion_service import PromotionService


def _pid(prefix: str = "PROMO") -> str:
    return f"{prefix}_TEST_{uuid.uuid4().hex[:8].upper()}"


class FakeLoyalty:
    """In-memory stand-in for the loyalty-cli HTTP surface.

    Remembers registered codes→OD so show_promo_code resolves like the real
    service. ``refuse`` makes the first OD register raise; ``issue_refuse_for``
    is a set of customer ids whose offer.issue is refused (already-issued sim).
    """

    def __init__(self, *, refuse: bool = False, issue_refuse_for: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._refuse = refuse
        self._issue_refuse_for = issue_refuse_for or set()
        self.codes: dict[str, str] = {}  # code -> offer_definition_id
        self.list_rows: list[dict] = []

    async def register_offer_definition(self, **kwargs):
        self.calls.append(("register_offer_definition", kwargs))
        if self._refuse:
            raise PolicyViolationFromServer(
                rule="offer_definition.register.duplicate",
                message="offer definition already exists",
                context={"source": "loyalty"},
            )
        return {"id": kwargs["definition_id"]}

    async def register_promo_code(self, **kwargs):
        self.calls.append(("register_promo_code", kwargs))
        self.codes[kwargs["code"]] = kwargs["offer_definition_id"]
        return {"code": kwargs["code"]}

    async def show_promo_code(self, code: str):
        self.calls.append(("show_promo_code", {"code": code}))
        if code not in self.codes:
            raise NotFound(f"no such code {code}")
        return {"offer_definition_id": self.codes[code], "state": "active"}

    async def issue_offer(self, **kwargs):
        self.calls.append(("issue_offer", kwargs))
        if kwargs["customer_id"] in self._issue_refuse_for:
            raise PolicyViolationFromServer(
                rule="offer.issue.already_issued",
                message="customer already has this offer",
                context={"source": "loyalty"},
            )
        return {"offer_id": kwargs["offer_id"], "state": "issued"}

    async def list_offers(self, **kwargs):
        self.calls.append(("list_offers", kwargs))
        return {"rows": self.list_rows, "limit": 50, "offset": 0, "has_more": False}

    def called(self, name: str) -> list[dict]:
        return [args for n, args in self.calls if n == name]


async def _cleanup(session: AsyncSession, *promotion_ids: str):
    await session.execute(
        text("DELETE FROM catalog.promotion WHERE id = ANY(:ids)"),
        {"ids": list(promotion_ids)},
    )
    await session.commit()


def _svc(session, loyalty, actor="admin"):
    return PromotionService(session, PromotionRepository(session), loyalty, actor)


class TestCreatePromotionTargeted:
    async def test_codeless_promo_goes_active_without_promo_code_register(
        self, db_session: AsyncSession
    ):
        pid = _pid("PROMO_VIP")
        loyalty = FakeLoyalty()
        try:
            promo = await _svc(db_session, loyalty).create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
            )
            assert promo.state == "active"
            assert promo.offer_definition_id == f"OD_{pid}"
            assert promo.code is None
            # targeted = codeless → OD registered, but NO promo code
            assert len(loyalty.called("register_offer_definition")) == 1
            assert loyalty.called("register_promo_code") == []
            # saga step 2 idempotency key is the promotion id
            assert loyalty.called("register_offer_definition")[0]["idempotency_key"] == pid
        finally:
            await _cleanup(db_session, pid)


class TestCreatePromotionNonTargeted:
    async def test_coded_promo_registers_code(self, db_session: AsyncSession):
        pid = _pid("PROMO_SUMMER")
        code = f"SUMMER_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            promo = await _svc(db_session, loyalty).create_promotion(
                promotion_id=pid,
                discount_type="absolute",
                discount_value=Decimal("5.00"),
                duration_kind="multi",
                periods_total=3,
                code=code,
                promo_code_kind="multi_use",
            )
            assert promo.state == "active"
            assert promo.code == code
            assert promo.periods_total == 3
            reg = loyalty.called("register_promo_code")
            assert len(reg) == 1
            assert reg[0]["code"] == code
            assert reg[0]["offer_definition_id"] == f"OD_{pid}"
            assert reg[0]["kind"] == "multi_use"
        finally:
            await _cleanup(db_session, pid)


class TestSagaFailureLeavesPendingLink:
    async def test_loyalty_refusal_translates_and_row_stays_pending(
        self, db_session: AsyncSession
    ):
        pid = _pid("PROMO_FAIL")
        loyalty = FakeLoyalty(refuse=True)
        try:
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, loyalty).create_promotion(
                    promotion_id=pid,
                    discount_type="percent",
                    discount_value=Decimal("10"),
                    duration_kind="single",
                )
            assert exc.value.rule == "catalog.promotion.loyalty_refused"
            # row written but NOT confirmed — harmless, reconcilable
            row = await PromotionRepository(db_session).get(pid)
            assert row is not None
            assert row.state == "pending_link"
            assert row.offer_definition_id is None
        finally:
            await _cleanup(db_session, pid)

    async def test_retry_resumes_pending_link_to_active(self, db_session: AsyncSession):
        pid = _pid("PROMO_RESUME")
        try:
            # first attempt fails at loyalty
            with pytest.raises(PolicyViolation):
                await _svc(db_session, FakeLoyalty(refuse=True)).create_promotion(
                    promotion_id=pid,
                    discount_type="percent",
                    discount_value=Decimal("15"),
                    duration_kind="single",
                )
            # retry with a healthy loyalty resumes the same row (no duplicate)
            healthy = FakeLoyalty()
            promo = await _svc(db_session, healthy).create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("15"),
                duration_kind="single",
            )
            assert promo.state == "active"
            assert promo.offer_definition_id == f"OD_{pid}"
            assert len(await PromotionRepository(db_session).list(limit=1000)) >= 1
        finally:
            await _cleanup(db_session, pid)


class TestCreatePromotionGuards:
    async def test_active_promo_rejects_duplicate(self, db_session: AsyncSession):
        pid = _pid("PROMO_DUP")
        try:
            await _svc(db_session, FakeLoyalty()).create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
            )
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, FakeLoyalty()).create_promotion(
                    promotion_id=pid,
                    discount_type="percent",
                    discount_value=Decimal("10"),
                    duration_kind="single",
                )
            assert exc.value.rule == "catalog.promotion.already_exists"
        finally:
            await _cleanup(db_session, pid)

    async def test_duplicate_code_rejected(self, db_session: AsyncSession):
        pid1, pid2 = _pid("PROMO_C1"), _pid("PROMO_C2")
        code = f"DUP_{uuid.uuid4().hex[:6].upper()}"
        try:
            await _svc(db_session, FakeLoyalty()).create_promotion(
                promotion_id=pid1,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
                code=code,
                promo_code_kind="single_use_shared",
            )
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, FakeLoyalty()).create_promotion(
                    promotion_id=pid2,
                    discount_type="percent",
                    discount_value=Decimal("10"),
                    duration_kind="single",
                    code=code,
                    promo_code_kind="single_use_shared",
                )
            assert exc.value.rule == "catalog.promotion.code_in_use"
        finally:
            await _cleanup(db_session, pid1, pid2)

    @pytest.mark.parametrize(
        "kwargs,expected_rule",
        [
            (dict(discount_type="bogus", discount_value=Decimal("10"), duration_kind="single"),
             "catalog.promotion.invalid_discount_type"),
            (dict(discount_type="percent", discount_value=Decimal("150"), duration_kind="single"),
             "catalog.promotion.invalid_discount_value"),
            (dict(discount_type="percent", discount_value=Decimal("0"), duration_kind="single"),
             "catalog.promotion.invalid_discount_value"),
            (dict(discount_type="percent", discount_value=Decimal("10"), duration_kind="multi"),
             "catalog.promotion.invalid_periods_total"),
            (dict(discount_type="percent", discount_value=Decimal("10"), duration_kind="single", periods_total=3),
             "catalog.promotion.invalid_periods_total"),
            (dict(discount_type="percent", discount_value=Decimal("10"), duration_kind="single", code="X"),
             "catalog.promotion.invalid_promo_code_kind"),
        ],
    )
    async def test_validation_rejects(self, db_session: AsyncSession, kwargs, expected_rule):
        pid = _pid("PROMO_VAL")
        try:
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, FakeLoyalty()).create_promotion(promotion_id=pid, **kwargs)
            assert exc.value.rule == expected_rule
        finally:
            await _cleanup(db_session, pid)

    async def test_non_admin_actor_rejected(self, db_session: AsyncSession):
        pid = _pid("PROMO_AUTH")
        with pytest.raises(PolicyViolation) as exc:
            await _svc(db_session, FakeLoyalty(), actor="anonymous").create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
            )
        assert exc.value.rule == "catalog.admin_only"


class TestValidateForOrder:
    async def test_valid_percent_composes_on_base(self, db_session: AsyncSession):
        pid = _pid("PROMO_VAL_OK")
        code = f"OK_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                code=code,
                promo_code_kind="multi_use",
            )
            r = await svc.validate_for_order(code=code, offering_id="PLAN_M")
            assert r["valid"] is True
            assert r["offer_definition_id"] == f"OD_{pid}"
            assert r["base"] > 0
            # effective = 20% off the base the catalog actually returned
            assert r["effective"] == (r["base"] * Decimal("0.80")).quantize(Decimal("0.01"))
            assert r["label"] == "20% off"
        finally:
            await _cleanup(db_session, pid)

    async def test_unknown_code(self, db_session: AsyncSession):
        r = await _svc(db_session, FakeLoyalty()).validate_for_order(
            code="NOPE", offering_id="PLAN_M"
        )
        assert r["valid"] is False
        assert r["reason"] == "unknown_code"

    async def test_not_applicable_to_offering(self, db_session: AsyncSession):
        pid = _pid("PROMO_VAL_NA")
        code = f"NA_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                applicable_offering_ids=["PLAN_S"],
                code=code,
                promo_code_kind="multi_use",
            )
            r = await svc.validate_for_order(code=code, offering_id="PLAN_M")
            assert r["valid"] is False
            assert r["reason"] == "not_applicable_to_offering"
        finally:
            await _cleanup(db_session, pid)

    async def test_expired_window(self, db_session: AsyncSession):
        pid = _pid("PROMO_VAL_EXP")
        code = f"EXP_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                valid_to=datetime.now(timezone.utc) - timedelta(days=1),
                code=code,
                promo_code_kind="multi_use",
            )
            r = await svc.validate_for_order(code=code, offering_id="PLAN_M")
            assert r["valid"] is False
            assert r["reason"] == "expired"
        finally:
            await _cleanup(db_session, pid)

    async def test_preview_returns_display_subset(self, db_session: AsyncSession):
        pid = _pid("PROMO_PREVIEW")
        code = f"PV_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="absolute",
                discount_value=Decimal("5.00"),
                duration_kind="single",
                code=code,
                promo_code_kind="multi_use",
            )
            p = await svc.preview_promo(code=code, offering_id="PLAN_M")
            assert set(p) == {"valid", "code", "offering_id", "label", "base", "effective", "reason"}
            assert p["valid"] is True
            assert p["label"] == "SGD 5.00 off"
            assert p["effective"] == p["base"] - Decimal("5.00")
        finally:
            await _cleanup(db_session, pid)


class TestAssignTargeted:
    async def test_issues_to_each_customer_skipping_duplicates(self, db_session: AsyncSession):
        pid = _pid("PROMO_ASSIGN")
        loyalty = FakeLoyalty(issue_refuse_for={"CUST-DUP"})
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(  # codeless = targeted
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
            )
            res = await svc.assign_targeted(
                promotion_id=pid, customer_ids=["CUST-1", "CUST-2", "CUST-DUP"]
            )
            assert set(res["issued"]) == {"CUST-1", "CUST-2"}
            assert [s["customer_id"] for s in res["skipped"]] == ["CUST-DUP"]
            issues = loyalty.called("issue_offer")
            assert len(issues) == 3
            # deterministic offer id == idempotency key (re-runnable)
            assert all(i["offer_id"] == i["idempotency_key"] for i in issues)
            assert issues[0]["offer_definition_id"] == f"OD_{pid}"
        finally:
            await _cleanup(db_session, pid)

    async def test_requires_active_promotion(self, db_session: AsyncSession):
        with pytest.raises(PolicyViolation) as exc:
            await _svc(db_session, FakeLoyalty()).assign_targeted(
                promotion_id=_pid("PROMO_GHOST"), customer_ids=["CUST-1"]
            )
        assert exc.value.rule == "catalog.promotion.not_active"


class TestListCustomerOffers:
    async def test_enriches_offers_with_promotion_terms(self, db_session: AsyncSession):
        pid = _pid("PROMO_LIST")
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("30"),
                duration_kind="single",
            )
            od = f"OD_{pid}"
            loyalty.list_rows = [
                {"offer_id": "OFF-1", "state": "issued", "offer_definition_id": od},
                {"offer_id": "OFF-2", "state": "issued", "offer_definition_id": "OD_UNKNOWN"},
            ]
            offers = await svc.list_customer_offers(customer_id="CUST-1", state="issued")
            assert len(offers) == 2
            enriched = next(o for o in offers if o["offer_id"] == "OFF-1")
            assert enriched["promotion"]["promotion_id"] == pid
            assert enriched["promotion"]["label"] == "30% off"
            unknown = next(o for o in offers if o["offer_id"] == "OFF-2")
            assert unknown["promotion"] is None
        finally:
            await _cleanup(db_session, pid)
