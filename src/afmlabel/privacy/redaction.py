# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Secret redaction for log records and error payloads.

Two mechanisms are combined:

* **value-based** -- every registered secret value (the ``HF_TOKEN``
  environment variable, the session signing secret, a password just
  received on the login endpoint, ...) is replaced wherever it appears;
* **pattern-based** -- well-known secret shapes (Hugging Face ``hf_...``
  tokens, ``Authorization: Bearer ...`` headers, ``password=...`` pairs,
  ``Cookie``/``Set-Cookie`` headers) are masked even when the value was
  never registered.

The redactor is intentionally simple and conservative: masking a little
too much is acceptable, leaking a secret is not.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

#: Environment variables whose *values* are always registered as secrets.
SECRET_ENV_VARS: tuple[str, ...] = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACE_TOKEN",
    "AFMLABEL_PASSWORD",
    "AFMLABEL_SESSION_SECRET",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Hugging Face user access tokens.
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), REDACTED),
    # Authorization headers of any scheme.
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic|token)?\s*[^\s,;'\"]+"),
        r"\1" + REDACTED,
    ),
    # key=value / key: value pairs for password-like keys (JSON, query, kwargs).
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|pwd|secret|session_secret|token|api_key|hf_token)"
            r"[\"']?\s*[:=]\s*)([\"']?)([^\s,;\"'}]+)(\2)"
        ),
        r"\1\2" + REDACTED + r"\4",
    ),
    # Cookie headers -- session identifiers.
    (re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"), r"\1" + REDACTED),
)


class SecretRegistry:
    """Thread-safe set of literal secret values to mask."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._values.add(value)

    def register_from_environment(self, names: Iterable[str] = SECRET_ENV_VARS) -> None:
        for name in names:
            self.register(os.environ.get(name))

    def values(self) -> list[str]:
        with self._lock:
            # Longest first so partial overlaps are masked fully.
            return sorted(self._values, key=len, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


registry = SecretRegistry()


def register_secret(value: str | None) -> None:
    registry.register(value)


def redact_text(text: str) -> str:
    for value in registry.values():
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside nested dict/list/tuple structures."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value


_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class RedactionFilter(logging.Filter):
    """Logging filter that masks secrets in the message, args and extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - formatting failure must not drop the record
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_ATTRS or key in ("msg", "args"):
                continue
            record.__dict__[key] = redact_value(value)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True
