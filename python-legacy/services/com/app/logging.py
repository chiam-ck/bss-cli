"""structlog configuration with redaction."""

import structlog

REDACTED_KEYS = frozenset({
    "token", "card_number", "cvv", "password", "secret",
    "document_number", "ki", "nric",
})


def _redact_sensitive(_, __, event_dict):
    for key in list(event_dict.keys()):
        if key in REDACTED_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.dev.set_exc_info,
            _redact_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(structlog, log_level.upper(), structlog.INFO) if hasattr(structlog, log_level.upper()) else 20
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
