"""v1.2 — outbox relay drains staged events and is the single publisher.

No real DB or RabbitMQ: a fake session feeds rows and records the mark-
published / mark-failed UPDATEs, and a fake exchange records (or fails) the
publish. Proves the two invariants that make delivery at-least-once:
- a staged row is published (routing_key = event_type, message_id = event_id)
  and then marked published_to_mq;
- a publish failure records last_publish_error and leaves the row UNpublished
  so the next tick retries it.
"""

import uuid

import pytest
from bss_events.relay import _drain_once


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.marked_ok = []
        self.marked_fail = []
        self.committed = False

    async def execute(self, sql, params=None):
        text = str(sql)
        if "FOR UPDATE SKIP LOCKED" in text:
            return _Result(rows=self._rows)
        if "last_publish_error" in text:
            self.marked_fail.append(params)
            return _Result(rowcount=1)
        # mark-ok UPDATE
        self.marked_ok.append(params)
        return _Result(rowcount=1)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSessionFactory:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self._session


class _FakeExchange:
    def __init__(self, fail=False):
        self.fail = fail
        self.published = []

    async def publish(self, message, routing_key):
        if self.fail:
            raise RuntimeError("broker down")
        self.published.append((routing_key, message))


def _row():
    return {
        "id": 1,
        "event_id": uuid.uuid4(),
        "event_type": "order.in_progress",
        "payload": {"commercialOrderId": "ORD-1"},
    }


@pytest.mark.asyncio
async def test_drain_publishes_and_marks():
    rows = [_row(), {**_row(), "id": 2, "event_type": "order.completed"}]
    session = _FakeSession(rows)
    exchange = _FakeExchange()

    drained = await _drain_once(_FakeSessionFactory(session), exchange, batch_size=100)

    assert drained == 2
    assert [rk for rk, _ in exchange.published] == ["order.in_progress", "order.completed"]
    # message_id carries the durable event_id (the inbox dedup key)
    assert exchange.published[0][1].message_id == str(rows[0]["event_id"])
    assert [p["id"] for p in session.marked_ok] == [1, 2]
    assert session.marked_fail == []
    assert session.committed


@pytest.mark.asyncio
async def test_publish_failure_records_error_and_leaves_unpublished():
    session = _FakeSession([_row()])
    exchange = _FakeExchange(fail=True)

    drained = await _drain_once(_FakeSessionFactory(session), exchange, batch_size=100)

    assert drained == 1
    assert session.marked_ok == []            # NOT marked published
    assert len(session.marked_fail) == 1      # recorded for retry
    assert "broker down" in session.marked_fail[0]["err"]
    assert session.committed
