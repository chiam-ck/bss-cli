"""File-system anchors for the package's templates and static assets.

Portal app factories use these to wire Jinja loaders and ``StaticFiles``
mounts that point at the bundled wheel resources rather than copies.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR: Path = _HERE / "templates"
STATIC_DIR: Path = _HERE / "static"
