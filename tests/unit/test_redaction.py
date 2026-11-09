# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import json
import logging

import pytest

from afmlabel.logging_config import JsonFormatter, configure_logging
from afmlabel.privacy.redaction import (
    REDACTED,
    RedactionFilter,
    redact_text,
    redact_value,
    register_secret,
    registry,
)
from afmlabel.settings import LoggingSettings


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    registry.clear()


def test_hugging_face_token_pattern_is_masked() -> None:
    text = "token hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 used"
    assert "hf_ABC" not in redact_text(text)
    assert REDACTED in redact_text(text)


def test_registered_secret_value_is_masked_anywhere() -> None:
    register_secret("s3cr3t-value-xyz")
    assert redact_text("prefix s3cr3t-value-xyz suffix") == f"prefix {REDACTED} suffix"


def test_environment_secret_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "envtokenvalue123")
    registry.register_from_environment()
    assert "envtokenvalue123" not in redact_text("HF_TOKEN=envtokenvalue123")


def test_authorization_header_is_masked() -> None:
    assert "abc.def" not in redact_text("Authorization: Bearer abc.def")
    assert "abc.def" not in redact_text("authorization=Basic abc.def")


def test_password_pairs_are_masked() -> None:
    assert "hunter2" not in redact_text("password=hunter2&x=1")
    assert "hunter2" not in redact_text('{"password": "hunter2"}')
    assert "hunter2" not in redact_text("session_secret: hunter2")


def test_cookie_headers_are_masked() -> None:
    assert "sid=abcdef" not in redact_text("Cookie: afmlabel_session=abcdef; sid=abcdef")
    assert "abcdef" not in redact_text("set-cookie: afmlabel_session=abcdef; HttpOnly")


def test_nested_structures_are_redacted() -> None:
    register_secret("topsecretvalue")
    value = {"a": ["topsecretvalue", {"b": "ok"}], "c": ("topsecretvalue",)}
    assert redact_value(value) == {"a": [REDACTED, {"b": "ok"}], "c": (REDACTED,)}


def test_logging_filter_and_formatter_redact_message_and_context() -> None:
    register_secret("logsecretvalue")
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "value=%s", ("logsecretvalue",), None
    )
    record.__dict__["context_token"] = "hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
    assert RedactionFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == f"value={REDACTED}"
    assert payload["context"]["context_token"] == REDACTED


def test_configure_logging_installs_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(json_output=True))
    logging.getLogger("afmlabel.test").warning("password=abcdef123")
    captured = capsys.readouterr().out
    assert "abcdef123" not in captured
    assert REDACTED in captured
