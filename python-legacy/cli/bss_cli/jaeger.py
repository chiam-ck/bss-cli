"""Re-export of the canonical Jaeger client from bss-telemetry.

The client moved to ``packages/bss-telemetry/`` so the orchestrator
can import it for the real ``trace.*`` tools. CLI commands keep
this short alias to avoid a noisy churn through ``bss_cli.commands.trace``.
"""

from bss_telemetry import JaegerClient, JaegerError

__all__ = ["JaegerClient", "JaegerError"]
