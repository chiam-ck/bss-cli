"""bss-admin — shared admin-api router factory.

Each BSS service mounts its own ``admin_router`` with a hardcoded
``ResetPlan`` for the single schema it owns. The Campaign OS schema is
physically unreachable because no BSS service would list it. Callers
coordinate resets across services via the CLI (``bss admin reset``) —
there is deliberately no cross-schema endpoint.
"""

from .reset import ResetPlan, TableReset, admin_router

__all__ = ["ResetPlan", "TableReset", "admin_router"]
