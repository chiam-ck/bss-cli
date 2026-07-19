"""Customer API tests — TMF629."""

from httpx import AsyncClient

PREFIX = "/tmf-api/customerManagement/v4"


class TestCreateCustomer:
    async def test_create_customer_happy(self, client: AsyncClient):
        r = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Test",
                "familyName": "User",
                "contactMedium": [
                    {"medium_type": "email", "value": "test.user@example.com", "is_primary": True}
                ],
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "active"
        assert body["kycStatus"] == "not_verified"
        assert body["@type"] == "Customer"
        assert "id" in body
        assert body["id"].startswith("CUST-")
        # v1.1.1 — the new customer is mirrored into loyalty's registry
        loyalty = client._transport.app.state.loyalty_client  # type: ignore[attr-defined]
        loyalty.register_customer.assert_awaited_once()
        assert loyalty.register_customer.await_args.kwargs["customer_id"] == body["id"]

    async def test_create_customer_no_contact_medium(self, client: AsyncClient):
        r = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "No",
                "familyName": "Contact",
                "contactMedium": [],
            },
        )
        assert r.status_code == 422
        body = r.json()
        assert body["reason"] == "customer.create.requires_contact_medium"

    async def test_create_customer_duplicate_email(self, client: AsyncClient):
        email = "duplicate.test@example.com"
        r1 = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "First",
                "familyName": "User",
                "contactMedium": [{"medium_type": "email", "value": email}],
            },
        )
        assert r1.status_code == 201

        r2 = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Second",
                "familyName": "User",
                "contactMedium": [{"medium_type": "email", "value": email}],
            },
        )
        assert r2.status_code == 422
        assert r2.json()["reason"] == "customer.create.email_unique"


class TestGetCustomer:
    async def test_get_customer_not_found(self, client: AsyncClient):
        r = await client.get(f"{PREFIX}/customer/CUST-NONEXISTENT")
        assert r.status_code == 404

    async def test_get_customer_after_create(self, client: AsyncClient):
        r = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Fetch",
                "familyName": "Me",
                "contactMedium": [{"medium_type": "email", "value": "fetch.me@example.com"}],
            },
        )
        cust_id = r.json()["id"]
        r2 = await client.get(f"{PREFIX}/customer/{cust_id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == cust_id


class TestFindCustomerByMsisdn:
    async def test_unassigned_msisdn_returns_404(self, client: AsyncClient):
        r = await client.get(f"{PREFIX}/customer/by-msisdn/99999999")
        assert r.status_code == 404

    async def test_resolves_msisdn_via_subscription_to_customer(
        self, client: AsyncClient, db_session
    ):
        from unittest.mock import AsyncMock

        # Create a customer.
        r = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Phone",
                "familyName": "Owner",
                "contactMedium": [
                    {"medium_type": "email", "value": "phone.owner@example.com"}
                ],
            },
        )
        customer_id = r.json()["id"]

        # Seed an MSISDN row pointing to a synthetic subscription id.
        from bss_models.inventory import MsisdnPool
        msisdn = "90008888"
        sub_id = "SUB-MOCK"
        db_session.add(
            MsisdnPool(
                msisdn=msisdn,
                status="assigned",
                assigned_to_subscription_id=sub_id,
                tenant_id="DEFAULT",
            )
        )
        await db_session.flush()

        # Patch the subscription_client this CRM app holds so the lookup
        # short-circuits to our customer.
        client._transport.app.state.subscription_client = AsyncMock()
        client._transport.app.state.subscription_client.get = AsyncMock(
            return_value={"id": sub_id, "customerId": customer_id}
        )

        r2 = await client.get(f"{PREFIX}/customer/by-msisdn/{msisdn}")
        assert r2.status_code == 200, r2.text
        assert r2.json()["id"] == customer_id

    async def test_subscription_lookup_failure_returns_404(
        self, client: AsyncClient, db_session
    ):
        from unittest.mock import AsyncMock

        from bss_models.inventory import MsisdnPool
        msisdn = "90007777"
        db_session.add(
            MsisdnPool(
                msisdn=msisdn,
                status="assigned",
                assigned_to_subscription_id="SUB-GHOST",
                tenant_id="DEFAULT",
            )
        )
        await db_session.flush()

        client._transport.app.state.subscription_client = AsyncMock()
        client._transport.app.state.subscription_client.get = AsyncMock(
            side_effect=RuntimeError("subscription service unreachable")
        )

        r = await client.get(f"{PREFIX}/customer/by-msisdn/{msisdn}")
        assert r.status_code == 404


class TestFindCustomerByEmail:
    async def test_unknown_email_returns_404(self, client: AsyncClient):
        r = await client.get(
            f"{PREFIX}/customer/by-email",
            params={"email": "nobody@nowhere.example"},
        )
        assert r.status_code == 404

    async def test_resolves_email_to_customer(self, client: AsyncClient):
        # Plus-addressing is the incident shape (chiamck+001@icloud.com)
        # — the query-param route must round-trip the '+' untouched.
        email = "by.email+001@example.com"
        r = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Email",
                "familyName": "Owner",
                "contactMedium": [
                    {"medium_type": "email", "value": email, "is_primary": True}
                ],
            },
        )
        customer_id = r.json()["id"]

        r2 = await client.get(
            f"{PREFIX}/customer/by-email", params={"email": email}
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["id"] == customer_id
        assert body["@type"] == "Customer"

    async def test_email_is_not_matched_by_name_filter(self, client: AsyncClient):
        # Pin the gap that motivated the route: list?name=<email> finds
        # nothing even when by-email resolves. If list ever starts
        # matching emails, this documents the overlap.
        email = "name.filter.gap@example.com"
        await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Gap",
                "familyName": "Case",
                "contactMedium": [
                    {"medium_type": "email", "value": email, "is_primary": True}
                ],
            },
        )
        r = await client.get(f"{PREFIX}/customer", params={"name": email})
        assert r.status_code == 200
        assert r.json() == []


class TestListCustomers:
    async def test_list_returns_populated_individual(self, client: AsyncClient):
        await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Lister",
                "familyName": "Alpha",
                "contactMedium": [{"medium_type": "email", "value": "lister.alpha@example.com"}],
            },
        )
        r = await client.get(f"{PREFIX}/customer")
        assert r.status_code == 200
        rows = r.json()
        assert rows, "expected at least one customer"
        row = next(r for r in rows if r["id"].startswith("CUST-"))
        assert row["individual"] is not None
        assert row["individual"]["givenName"]
        assert row["individual"]["familyName"]

    async def test_list_filters_by_name_case_insensitive(self, client: AsyncClient):
        await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Zephyr",
                "familyName": "Quark",
                "contactMedium": [{"medium_type": "email", "value": "zephyr.q@example.com"}],
            },
        )
        await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Other",
                "familyName": "Person",
                "contactMedium": [{"medium_type": "email", "value": "other.p@example.com"}],
            },
        )

        r = await client.get(f"{PREFIX}/customer", params={"name": "zephyr"})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["individual"]["givenName"] == "Zephyr"

        r2 = await client.get(f"{PREFIX}/customer", params={"name": "QUARK"})
        assert r2.status_code == 200
        assert len(r2.json()) == 1

        r3 = await client.get(f"{PREFIX}/customer", params={"name": "nonexistent-xyz"})
        assert r3.status_code == 200
        assert r3.json() == []

    async def test_list_status_filter_still_works(self, client: AsyncClient):
        r = await client.get(f"{PREFIX}/customer", params={"status": "active"})
        assert r.status_code == 200
        for row in r.json():
            assert row["status"] == "active"


class TestContactMedium:
    async def test_add_contact_medium(self, client: AsyncClient):
        r = await client.post(
            f"{PREFIX}/customer",
            json={
                "givenName": "Contact",
                "familyName": "Test",
                "contactMedium": [{"medium_type": "email", "value": "contact.test@example.com"}],
            },
        )
        cust_id = r.json()["id"]

        r2 = await client.post(
            f"{PREFIX}/customer/{cust_id}/contactMedium",
            json={"mediumType": "phone", "value": "+6590001234"},
        )
        assert r2.status_code == 201
        assert r2.json()["mediumType"] == "phone"
