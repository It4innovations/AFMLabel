# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Structured (JSON) logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from afmlabel.privacy.redaction import RedactionFilter, redact_text, registry
from afmlabel.settings import LoggingSettings

_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS
        }
        if extra:
            payload["context"] = extra
        return redact_text(json.dumps(payload, default=str))


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


_POLLING_PATH_PREFIXES = (
    ("GET", "/api/v1/system/resources"),
    ("GET", "/api/v1/models"),
    ("GET", "/api/v1/projects/", "/jobs/"),
    ("POST", "/api/v1/projects/", "/heartbeat"),
)


def is_polling_request(method: str, path: str, status_code: int) -> bool:
    """Successful periodic browser polls that carry no user action."""
    if status_code >= 300:
        return False
    for rule in _POLLING_PATH_PREFIXES:
        if method != rule[0] or not path.startswith(rule[1]):
            continue
        if len(rule) == 2:
            if path == rule[1]:
                return True
        elif rule[2] in path:
            return True
    return False


class PollingAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for successful periodic polls."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method, path, status = args[1], args[2], args[4]
            try:
                return not is_polling_request(str(method), str(path), int(str(status)))
            except (TypeError, ValueError):
                return True
        return True


def configure_logging(settings: LoggingSettings | None = None) -> None:
    settings = settings or LoggingSettings()
    registry.register_from_environment()
    root = logging.getLogger()
    root.setLevel(settings.level.upper())
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(RedactionFilter())
    if settings.json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            RedactingTextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root.addHandler(handler)
    # uvicorn installs its own handlers; route them through the redacting root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    for existing in list(access.filters):
        if isinstance(existing, PollingAccessFilter):
            access.removeFilter(existing)
    if not settings.log_polling_requests:
        access.addFilter(PollingAccessFilter())
