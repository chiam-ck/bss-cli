#!/usr/bin/env python3
"""Capture a `bss trace` swimlane as a PNG via playwright + HTML wrapper.

Avoids needing scrot/maim/X11. Runs the trace command, captures the
plain ASCII output, wraps in dark-themed monospace HTML, and screenshots
the rendered page headlessly.

Run from repo root:
    uv run python docs/screenshots/capture_trace.py [ORDER_ID]

If ORDER_ID is omitted, looks up the latest order on customer "Ck Demo".
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "bss_trace_swimlane_v0_2.png"
TOKEN_LINE = next(
    (ln for ln in Path(".env").read_text().splitlines() if ln.startswith("BSS_API_TOKEN=")),
    "",
)
TOKEN = TOKEN_LINE.split("=", 1)[1] if "=" in TOKEN_LINE else ""


def _resolve_order_id(arg: str | None) -> str:
    if arg:
        return arg
    # Look up the most recent customer named "Ck Demo" → its newest order
    headers = {"X-BSS-API-Token": TOKEN}
    customers = httpx.get(
        "http://localhost:8002/tmf-api/customerManagement/v4/customer",
        params={"name": "Ck Demo", "limit": 5},
        headers=headers,
        timeout=10,
    ).json()
    if not customers:
        raise SystemExit("No customer matching 'Ck Demo' — run scenarios/customer_signup_and_exhaust.yaml first.")
    cust_id = customers[-1]["id"]
    orders = httpx.get(
        "http://localhost:8004/tmf-api/productOrderingManagement/v4/productOrder",
        params={"customerId": cust_id},
        headers=headers,
        timeout=10,
    ).json()
    if not orders:
        raise SystemExit(f"No orders for {cust_id}")
    return orders[-1]["id"]


def _render_trace(order_id: str) -> str:
    out = subprocess.run(
        ["uv", "run", "bss", "trace", "for-order", order_id, "--show-sql"],
        capture_output=True,
        text=True,
        env={**os.environ, "TERM": "xterm", "COLUMNS": "160"},
        timeout=30,
    )
    if out.returncode != 0:
        raise SystemExit(f"bss trace failed:\n{out.stderr}")
    return out.stdout


def _wrap_html(trace_text: str, order_id: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>bss trace {order_id}</title>
<style>
  html, body {{
    background: #0e1014;
    color: #d8d8d4;
    font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    font-size: 12px;
    line-height: 1.4;
    margin: 0;
    padding: 16px 24px;
  }}
  pre {{
    margin: 0;
    white-space: pre;
    overflow-x: auto;
  }}
</style></head>
<body><pre>{html.escape(trace_text)}</pre></body></html>"""


def main(argv: list[str]) -> int:
    order_id = _resolve_order_id(argv[1] if len(argv) > 1 else None)
    print(f"trace for: {order_id}")
    trace_text = _render_trace(order_id)
    html_path = OUT.with_suffix(".html")
    html_path.write_text(_wrap_html(trace_text, order_id))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            color_scheme="dark",
        )
        page = ctx.new_page()
        page.goto(f"file://{html_path.resolve()}")
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT), full_page=True)
        browser.close()
    html_path.unlink(missing_ok=True)
    if shutil.which("oxipng"):
        subprocess.run(["oxipng", "-o", "4", "--quiet", str(OUT)], check=False)
    print(f"captured: {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
