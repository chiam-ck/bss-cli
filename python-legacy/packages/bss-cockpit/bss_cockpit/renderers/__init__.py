"""ASCII renderers for CLI + LLM context feedback.

Each renderer takes already-fetched dict payloads and returns a plain ``str``
block (no ``rich`` markup in the return value — renderers are also fed back
into the LLM's context, so they must be pure text).

Six hero renderers (section 7 of PHASE_09.md):
    1. subscription.render_subscription          — bundle bars, state, countdown
    2. customer.render_customer_360              — 360 view
    3. case.render_case                          — case + child tickets
    4. order.render_order                        — order + SOM tree
    5. catalog.render_catalog                    — 3-column plan comparison
    6. esim.render_esim_activation               — activation card with QR

Plus two simpler table renderers (ticket list, prov tasks).
"""

from .case import render_case
from .catalog import render_catalog, render_catalog_show, render_vas_list
from .customer import render_customer_360
from .dispatch import RENDERER_DISPATCH, render_tool_result
from .esim import render_esim_activation
from .inventory import render_msisdn_count, render_msisdn_list
from .order import render_order
from .port_request import render_port_request_get, render_port_request_list
from .prov import render_prov_tasks
from .subscription import render_subscription
from .ticket import render_ticket

__all__ = [
    "RENDERER_DISPATCH",
    "render_case",
    "render_catalog",
    "render_catalog_show",
    "render_customer_360",
    "render_esim_activation",
    "render_msisdn_count",
    "render_msisdn_list",
    "render_order",
    "render_port_request_get",
    "render_port_request_list",
    "render_prov_tasks",
    "render_subscription",
    "render_ticket",
    "render_tool_result",
    "render_vas_list",
]
