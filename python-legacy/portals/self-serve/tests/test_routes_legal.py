"""Public legal pages — /terms and /privacy (v0.12 PR20).

Both must:
* Return 200 without an active session (public allowlist).
* Render the canonical headings.
* Cross-link to each other in the footer note.
"""

from __future__ import annotations


def test_terms_is_public_and_renders(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/terms")
    assert r.status_code == 200
    body = r.text
    assert "Terms of Service" in body
    assert "Acceptance of terms" in body
    assert 'href="/privacy"' in body  # cross-link


def test_privacy_is_public_and_renders(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/privacy")
    assert r.status_code == 200
    body = r.text
    assert "Privacy Policy" in body
    assert "What we collect" in body
    assert 'href="/terms"' in body  # cross-link


def test_terms_and_privacy_in_public_allowlist() -> None:
    from bss_self_serve.security import PUBLIC_EXACT_PATHS

    assert "/terms" in PUBLIC_EXACT_PATHS
    assert "/privacy" in PUBLIC_EXACT_PATHS


def test_footer_links_render_on_every_page(client) -> None:  # type: ignore[no-untyped-def]
    """The base.html footer carries Terms + Privacy links so a
    visitor can reach them from any page."""
    for path in ("/welcome", "/plans", "/terms", "/privacy"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} not reachable"
        assert 'href="/terms"' in r.text
        assert 'href="/privacy"' in r.text
