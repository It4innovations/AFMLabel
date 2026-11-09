# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import logging

from afmlabel.logging_config import PollingAccessFilter, configure_logging, is_polling_request
from afmlabel.settings import LoggingSettings


def test_polling_requests_are_recognised() -> None:
    assert is_polling_request("GET", "/api/v1/system/resources", 200)
    assert is_polling_request("GET", "/api/v1/models", 200)
    assert is_polling_request("GET", "/api/v1/projects/p1/jobs/j1", 200)
    assert is_polling_request("POST", "/api/v1/projects/p1/datasets/d1/lock/heartbeat", 200)
    assert is_polling_request("POST", "/api/v1/projects/p1/lock/heartbeat", 204)
    # User actions and failures stay in the log.
    assert not is_polling_request("POST", "/api/v1/models/sam21/load", 202)
    assert not is_polling_request("GET", "/api/v1/system/resources", 500)
    assert not is_polling_request("GET", "/api/v1/projects/p1/jobs", 200)
    assert not is_polling_request("POST", "/api/v1/projects/p1/datasets/d1/segment", 202)
    assert not is_polling_request("GET", "/api/v1/projects/p1", 200)


def _access_record(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", method, path, "1.1", status),
        None,
    )


def test_filter_drops_only_polls() -> None:
    flt = PollingAccessFilter()
    assert flt.filter(_access_record("GET", "/api/v1/system/resources", 200)) is False
    assert flt.filter(_access_record("GET", "/projects/p1", 200)) is True
    plain = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "hello", (), None)
    assert flt.filter(plain) is True


def test_configure_logging_installs_filter_unless_disabled() -> None:
    access = logging.getLogger("uvicorn.access")
    configure_logging(LoggingSettings(json_output=False))
    assert any(isinstance(f, PollingAccessFilter) for f in access.filters)
    configure_logging(LoggingSettings(json_output=False, log_polling_requests=True))
    assert not any(isinstance(f, PollingAccessFilter) for f in access.filters)
    configure_logging(LoggingSettings(json_output=False))
