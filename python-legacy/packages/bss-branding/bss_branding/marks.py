"""Built-in logo marks + validation for operator-typed custom marks.

The mark is the text glyph that renders wherever an image can't: the
CLI banner, email headers, and either portal when no logo image has
been uploaded. It is operator input that flows into f-string email
HTML, so validation here is a security boundary, not cosmetics —
Jinja autoescape covers the portals, but the email renderer builds
HTML by hand.
"""

from __future__ import annotations

LOGO_MARKS: tuple[str, ...] = ("$", "●", "▲", "✦", "►")

_FORBIDDEN_CHARS = frozenset("<>&\"'")


def validate_mark(value: str) -> str:
    """Return the stripped mark or raise ``ValueError``.

    1–3 printable characters; HTML-active characters are rejected
    outright (belt-and-braces on top of escaping at render time).
    """
    mark = value.strip()
    if not 1 <= len(mark) <= 3:
        raise ValueError("mark must be 1-3 characters")
    if not mark.isprintable():
        raise ValueError("mark must be printable characters only")
    if _FORBIDDEN_CHARS & set(mark):
        raise ValueError("mark must not contain <, >, &, quotes")
    return mark
