"""BSS-CLI — Typer CLI + LLM REPL entry point.

v0.19 — suppress the noisy `langchain_core.deprecation` UserWarning
that fires on every `bss` invocation under Python 3.14 ("Core
Pydantic V1 functionality isn't compatible with Python 3.14 or
greater"). The warning comes from langchain importing
`pydantic.v1.fields.FieldInfo` for back-compat shims — orthogonal
to anything we control. Filtering at the package import level
ensures it's silenced before any `bss_orchestrator` / langchain
code lands in the REPL banner output.
"""

import warnings

warnings.filterwarnings(
    "ignore",
    message=r".*Core Pydantic V1 functionality isn't compatible.*",
    category=UserWarning,
    module=r"langchain_core\..*",
)
