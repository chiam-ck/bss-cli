import logging

import structlog

REDACTED_KEYS = frozenset({
    "document_number", "cvv", "card_number", "ki", "pan", "password", "token",
})
SAFE_SUFFIXES = ("_ref", "_id")


def redact_sensitive(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    for key in list(event_dict):
        if key in REDACTED_KEYS and not any(key.endswith(s) for s in SAFE_SUFFIXES):
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            redact_sensitive,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
