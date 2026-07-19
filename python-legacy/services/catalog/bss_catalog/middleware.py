"""Request middleware — pure ASGI to preserve contextvar propagation.

BaseHTTPMiddleware runs the inner app in a separate asyncio task,
breaking ContextVar propagation including OTel's current span
context. Pure ASGI middleware preserves it.
"""

import json
import uuid

import structlog
from bss_telemetry import stamp_request_span
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bss_catalog import auth_context
from bss_catalog.policies import PolicyViolation

log = structlog.get_logger()


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
        }
        request_id = headers.get("x-request-id") or str(uuid.uuid4())
        actor = headers.get("x-bss-actor", "system")
        channel = headers.get("x-bss-channel", "system")
        tenant = headers.get("x-bss-tenant", "DEFAULT")
        # v0.9 — service_identity is set by BSSApiTokenMiddleware after
        # token validation. Default fallback is for tests that bypass
        # the perimeter middleware (direct ASGITransport against the app).
        service_identity = scope.get("service_identity", "default")

        auth_context.set_for_request(
            actor=actor,
            tenant=tenant,
            channel=channel,
            service_identity=service_identity,
        )

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            actor=actor,
            channel=channel,
            service_identity=service_identity,
            method=scope.get("method", "?"),
            path=scope.get("path", "?"),
        )

        # v0.9 — stamp the active server span with caller context.
        stamp_request_span(
            actor=actor,
            channel=channel,
            service_identity=service_identity,
        )

        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 0)
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers_list}
            await send(message)

        log.info("request.start")
        try:
            await self.app(scope, receive, send_wrapper)
        except PolicyViolation as exc:
            log.warning(
                "policy.violation",
                rule=exc.rule,
                message=exc.message,
                context=exc.context,
            )
            body = json.dumps({
                "code": "POLICY_VIOLATION",
                "reason": exc.rule,
                "message": exc.message,
                "referenceError": f"https://docs.bss-cli.dev/policies/{exc.rule}",
                "context": exc.context,
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 422,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-request-id", request_id.encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        log.info("request.end", status=status_holder["status"])
