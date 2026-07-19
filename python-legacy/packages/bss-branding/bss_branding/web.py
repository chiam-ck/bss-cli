"""HTTP helper for serving the uploaded logo. Starlette is imported
lazily so the package core stays framework-free (the email renderer in
``bss-portal-auth`` and the CLI must not drag starlette in)."""

from __future__ import annotations

from .assets import CONTENT_TYPE_BY_FILENAME
from .config import current


def logo_response():  # -> starlette.responses.Response
    """404 when no logo is configured/present; else the image bytes.

    The URL carries ``?v=<mtime>`` so aggressive caching is safe: a
    replaced logo gets a new URL, the old one may live in cache
    forever.
    """
    from starlette.responses import Response

    view = current()
    if view.logo_path is None:
        return Response(status_code=404)

    data = view.logo_path.read_bytes()
    content_type = CONTENT_TYPE_BY_FILENAME.get(view.logo_path.name, "application/octet-stream")
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{view.logo_version}-{len(data)}"',
        },
    )
