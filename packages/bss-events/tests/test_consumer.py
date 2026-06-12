"""v1.2 — safe-consumer helper primitives: inbox dedup + retry counting.

The full RabbitMQ topology (DLX, retry TTL, parked queue) is exercised in
integration tests against a live broker; here we unit-test the two pure
decision points the helper hangs on:
- the inbox claim distinguishes a first delivery from a redelivery;
- the x-death header is parsed into the retry count that drives retry-vs-park.
"""

import pytest
from bss_events.consumer import _claim_inbox, _death_count


class _Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, rowcount):
        self._rowcount = rowcount
        self.last_sql = None
        self.last_params = None

    async def execute(self, sql, params=None):
        self.last_sql = str(sql)
        self.last_params = params
        return _Result(self._rowcount)


class _FakeMessage:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.asyncio
async def test_claim_inbox_first_delivery_is_claimed():
    session = _FakeSession(rowcount=1)
    claimed = await _claim_inbox(session, "order_mgmt", "com.x", "evt-1")
    assert claimed is True
    assert "order_mgmt.processed_event" in session.last_sql
    assert "ON CONFLICT" in session.last_sql
    assert session.last_params == {"event_id": "evt-1", "consumer": "com.x"}


@pytest.mark.asyncio
async def test_claim_inbox_redelivery_is_skipped():
    session = _FakeSession(rowcount=0)  # ON CONFLICT DO NOTHING → no row
    claimed = await _claim_inbox(session, "order_mgmt", "com.x", "evt-1")
    assert claimed is False


def test_death_count_none_when_no_header():
    assert _death_count(_FakeMessage(None)) == 0
    assert _death_count(_FakeMessage({})) == 0


def test_death_count_reads_xdeath():
    msg = _FakeMessage({"x-death": [{"count": 3, "queue": "com.x"}]})
    assert _death_count(msg) == 3


def test_death_count_tolerates_malformed_header():
    assert _death_count(_FakeMessage({"x-death": []})) == 0
    assert _death_count(_FakeMessage({"x-death": [{}]})) == 0
