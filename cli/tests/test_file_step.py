"""Unit tests for the v0.8 ``file:`` scenario step."""

from __future__ import annotations

import pytest
from bss_cli.scenarios.context import ScenarioContext
from bss_cli.scenarios.file_step import run_file_step
from bss_cli.scenarios.schema import FileReadStep, HTTPRegexCapture


def _format_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@pytest.mark.asyncio
async def test_file_step_reads_and_captures(tmp_path):
    mailbox = tmp_path / "mail.log"
    mailbox.write_text(
        "=== 2026-04-26T10:00:00 ===\n"
        "To: ada@x.sg\n"
        "OTP: 111111\n"
        "Magic link: ABCDEF\n"
        "=== 2026-04-26T10:05:00 ===\n"
        "To: ada@x.sg\n"
        "OTP: 222222\n"
        "Magic link: GHIJKL\n"
    )
    step = FileReadStep(
        name="read mailbox",
        file=str(mailbox),
        capture_regex={
            "otp": HTTPRegexCapture(source="body_text", pattern=r"OTP: (\d{6})"),
            "magic": HTTPRegexCapture(source="body_text", pattern=r"Magic link: (\w+)"),
        },
    )
    ctx = ScenarioContext.new()
    res = await run_file_step(step, ctx, format_error=_format_error)
    assert res.ok
    # Last match wins — picks the most recent OTP from the file.
    assert ctx.variables["otp"] == "222222"
    assert ctx.variables["magic"] == "GHIJKL"


@pytest.mark.asyncio
async def test_file_step_polls_until_file_appears(tmp_path):
    """Common race: portal writes the mailbox milliseconds after the POST."""
    import asyncio

    from bss_cli.scenarios.schema import Poll

    mailbox = tmp_path / "delayed.log"

    async def _write_later():
        await asyncio.sleep(0.15)
        mailbox.write_text("OTP: 424242\n")

    step = FileReadStep(
        name="read mailbox",
        file=str(mailbox),
        capture_regex={
            "otp": HTTPRegexCapture(source="body_text", pattern=r"OTP: (\d{6})"),
        },
        poll=Poll(interval_ms=50, timeout_seconds=1.0),
    )
    ctx = ScenarioContext.new()
    write_task = asyncio.create_task(_write_later())
    res = await run_file_step(step, ctx, format_error=_format_error)
    await write_task
    assert res.ok
    assert ctx.variables["otp"] == "424242"


@pytest.mark.asyncio
async def test_file_step_fails_when_pattern_missing(tmp_path):
    mailbox = tmp_path / "mail.log"
    mailbox.write_text("Hello, no OTP here.\n")
    step = FileReadStep(
        name="read mailbox",
        file=str(mailbox),
        capture_regex={
            "otp": HTTPRegexCapture(source="body_text", pattern=r"OTP: (\d{6})"),
        },
    )
    ctx = ScenarioContext.new()
    res = await run_file_step(step, ctx, format_error=_format_error)
    assert not res.ok
    assert "did not match" in res.error
