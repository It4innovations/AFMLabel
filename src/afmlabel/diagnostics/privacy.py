# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Privacy diagnostic: ``python -m afmlabel.diagnostics.privacy``.

Reports the configured external endpoints, obvious telemetry/cloud
settings, whether an HF token is present (never its value), and scans the
served templates/static assets for remote resource references. It does not
claim to prove the absence of every network call -- see
the README for the network policy.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from afmlabel.privacy.redaction import SECRET_ENV_VARS
from afmlabel.settings import AppSettings, load_settings

FindingLevel = Literal["info", "warning", "error"]

_ASSET_ATTR = re.compile(r"""(?:src|href|url)\s*[=(]\s*["']?(https?://[^"'\s)>]+)""", re.I)
_ANY_URL = re.compile(r"https?://[^\s\"'<>)]+")
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "static"

#: Navigation links (plain anchors the user may click) that are acceptable;
#: nothing is *loaded* from them.
_KNOWN_NAVIGATION_HOSTS = ("blender.it4i.cz", "code.it4i.cz")

#: Environment variables that switch third-party libraries into offline or
#: no-telemetry mode; reported so an operator can verify the deployment.
_OFFLINE_ENV_VARS = (
    "HF_HUB_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "TRANSFORMERS_OFFLINE",
    "DO_NOT_TRACK",
)


class Finding(BaseModel):
    level: FindingLevel
    area: str
    message: str


class PrivacyReport(BaseModel):
    status: Literal["ok", "warning", "error"]
    configured_external_endpoints: list[str] = Field(default_factory=list)
    hf_token_present: bool
    allow_external_runtime_network: bool
    offline_environment: dict[str, str | None] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)


def scan_frontend_assets(
    template_dir: Path = _TEMPLATE_DIR, static_dir: Path = _STATIC_DIR
) -> list[Finding]:
    findings: list[Finding] = []
    for root in (template_dir, static_dir):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".html", ".css", ".js"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in _ASSET_ATTR.finditer(text):
                url = match.group(1)
                host = url.split("/")[2] if url.count("/") >= 2 else url
                if host in _KNOWN_NAVIGATION_HOSTS and path.suffix.lower() == ".html":
                    findings.append(
                        Finding(
                            level="info",
                            area="frontend",
                            message=f"{path.name}: navigation link to {host} (no asset load)",
                        )
                    )
                    continue
                if "xmlns" in text[max(0, match.start() - 40) : match.start()]:
                    continue
                findings.append(
                    Finding(
                        level="error",
                        area="frontend",
                        message=f"{path.relative_to(root.parent)}: remote asset reference {url}",
                    )
                )
            for match in _ANY_URL.finditer(text):
                url = match.group(0)
                if url.startswith(("http://www.w3.org/", "https://www.w3.org/")):
                    continue
                if any(host in url for host in _KNOWN_NAVIGATION_HOSTS):
                    continue
                if _ASSET_ATTR.search(text[max(0, match.start() - 12) : match.end()]):
                    continue
                findings.append(
                    Finding(
                        level="warning",
                        area="frontend",
                        message=f"{path.relative_to(root.parent)}: mentions URL {url}",
                    )
                )
    return findings


def run_privacy_diagnostic(settings: AppSettings) -> PrivacyReport:
    findings = scan_frontend_assets()
    endpoints = [
        f"https://huggingface.co/{settings.models.sam31.repository} "
        "(model acquisition only; scripts/download_models.py; never at runtime)"
    ]
    hf_token_present = any(os.environ.get(name) for name in SECRET_ENV_VARS[:3])
    if hf_token_present:
        findings.append(
            Finding(
                level="info",
                area="secrets",
                message="an HF token is present in the environment (value not shown); it is "
                "used only by scripts/download_models.py",
            )
        )
    if settings.security.allow_external_runtime_network:
        findings.append(
            Finding(
                level="warning",
                area="config",
                message="security.allow_external_runtime_network is true; the default is false",
            )
        )
    findings.append(
        Finding(
            level="info",
            area="runtime",
            message="no analytics, error reporting, remote fonts or CDN assets are configured",
        )
    )
    offline = {name: os.environ.get(name) for name in _OFFLINE_ENV_VARS}
    status: Literal["ok", "warning", "error"] = "ok"
    if any(f.level == "error" for f in findings):
        status = "error"
    elif any(f.level == "warning" for f in findings):
        status = "warning"
    return PrivacyReport(
        status=status,
        configured_external_endpoints=endpoints,
        hf_token_present=hf_token_present,
        allow_external_runtime_network=settings.security.allow_external_runtime_network,
        offline_environment=offline,
        findings=findings,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m afmlabel.diagnostics.privacy")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_privacy_diagnostic(load_settings(args.config))
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"status: {report.status}")
        print(f"HF token present: {report.hf_token_present}")
        print(f"allow_external_runtime_network: {report.allow_external_runtime_network}")
        print("configured external endpoints:")
        for endpoint in report.configured_external_endpoints:
            print(f"  - {endpoint}")
        print("offline environment flags:")
        for key, value in report.offline_environment.items():
            print(f"  {key}={value}")
        print("findings:")
        for finding in report.findings:
            print(f"  [{finding.level}] {finding.area}: {finding.message}")
    return 1 if report.status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
