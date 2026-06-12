"""Doctrine guards for the v0.13 retirement of CSR staff auth.

phases/V0_13_0.md "Doctrine guards" — every assertion below must
hold for v0.13 to be considered shipped:

* /login returns 404 (route is gone).
* OperatorSessionStore is gone (greppable, no Python imports).
* require_operator dependency is gone.
* AgentAskStore + ask_about_customer are gone.
* astream_once appears in only one routes file (cockpit.py) on the
  CSR side — same shape as the v0.12 guard for self-serve chat.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_login_route_returns_404() -> None:
    app = create_app(Settings())
    with TestClient(app) as c:
        r = c.get("/login")
        assert r.status_code == 404


def test_operator_session_store_grep_empty() -> None:
    """No reference to the retired OperatorSessionStore class.

    Search the CSR portal tree for any leftover imports / references.
    The lone exception we accept is this test file (which has to name
    the symbol to assert it's gone).
    """
    out = subprocess.run(
        [
            "grep", "-rn", "OperatorSessionStore",
            "--include=*.py",
            str(_REPO_ROOT / "portals" / "csr" / "bss_csr"),
        ],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == ""


def test_require_operator_grep_empty() -> None:
    out = subprocess.run(
        [
            "grep", "-rn", "require_operator",
            "--include=*.py",
            str(_REPO_ROOT / "portals" / "csr" / "bss_csr"),
        ],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == ""


def test_agent_ask_store_grep_empty() -> None:
    out = subprocess.run(
        [
            "grep", "-rn", "AgentAskStore",
            "--include=*.py",
            str(_REPO_ROOT / "portals" / "csr" / "bss_csr"),
        ],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == ""


def test_ask_about_customer_grep_empty() -> None:
    out = subprocess.run(
        [
            "grep", "-rn", "ask_about_customer",
            "--include=*.py",
            str(_REPO_ROOT / "portals" / "csr" / "bss_csr"),
        ],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == ""


def test_astream_once_only_in_cockpit_route() -> None:
    """astream_once must live only in cockpit.py — same doctrine guard
    self-serve chat enforces for routes/chat.py."""
    out = subprocess.run(
        [
            "grep", "-rn", "astream_once",
            "--include=*.py",
            str(_REPO_ROOT / "portals" / "csr" / "bss_csr" / "routes"),
        ],
        capture_output=True, text=True,
    )
    lines = [
        line for line in out.stdout.strip().split("\n") if line
    ]
    # Every line must come from cockpit.py.
    for line in lines:
        assert "/routes/cockpit.py:" in line, (
            f"astream_once leaked into a non-cockpit route: {line}"
        )
