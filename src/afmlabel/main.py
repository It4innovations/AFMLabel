# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""FastAPI application factory and the ``afmlabel`` CLI entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import mimetypes
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from afmlabel import __version__
from afmlabel.annotations.service import RegionService, count_regions
from afmlabel.api.auth import router as auth_router
from afmlabel.api.datasets import router as datasets_router
from afmlabel.api.errors import install_error_handlers
from afmlabel.api.exports import router as exports_router
from afmlabel.api.health import router as health_router
from afmlabel.api.help import router as help_router
from afmlabel.api.imports import router as imports_router
from afmlabel.api.pages import router as pages_router
from afmlabel.api.projects import router as projects_router
from afmlabel.api.regions import router as regions_router
from afmlabel.api.segmentation import router as segmentation_router
from afmlabel.api.validation import router as validation_router
from afmlabel.auth.account import AccountStore
from afmlabel.auth.middleware import AuthMiddleware
from afmlabel.auth.passwords import WeakPasswordError
from afmlabel.auth.sessions import LoginRateLimiter, SessionStore
from afmlabel.exports.service import ExportService
from afmlabel.imports.batches import BatchStore
from afmlabel.imports.tswat import TswatImportService
from afmlabel.jobs.manager import JobManager
from afmlabel.logging_config import configure_logging
from afmlabel.privacy.redaction import register_secret
from afmlabel.projects.datasets import DatasetStore
from afmlabel.projects.store import ProjectStore
from afmlabel.resources.monitor import ResourceMonitor
from afmlabel.segmentation.registry import build_registry
from afmlabel.settings import AppSettings, load_settings
from afmlabel.templating import static_files
from afmlabel.validation.service import ValidationService

logger = logging.getLogger(__name__)

# StaticFiles derives Content-Type from the stdlib ``mimetypes`` module, which
# on Windows seeds itself from the registry where ``.js`` is sometimes
# "text/plain"; browsers then refuse to execute ES modules. Pin the types.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.logging)
    settings.workspace.root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            application.state.job_manager.shutdown()
            application.state.registry.release_all()

    app = FastAPI(
        title="AFMLabel", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.started_at_monotonic = time.monotonic()
    app.state.settings = settings
    app.state.registry = build_registry(settings)
    app.state.resource_monitor = ResourceMonitor(
        settings.workspace.root,
        refresh_seconds=settings.runtime.resource_refresh_seconds,
        low_ram_warning_gb=settings.resources.low_ram_warning_gb,
        low_disk_warning_gb=settings.resources.low_disk_warning_gb,
    )
    app.state.accounts = AccountStore(settings.workspace.root)
    app.state.sessions = SessionStore(
        settings.workspace.root, timeout_minutes=settings.security.session_timeout_minutes
    )
    app.state.login_limiter = LoginRateLimiter(
        limit=settings.security.login_rate_limit,
        window_seconds=settings.security.login_rate_window_seconds,
    )
    app.state.project_store = ProjectStore(settings.workspace.root)
    app.state.project_store.recover_incomplete_writes()
    app.state.batch_store = BatchStore(
        settings.workspace.root,
        max_file_size_bytes=int(settings.upload.maximum_file_size_mb * 1024 * 1024),
        chunk_size=settings.upload.chunk_size_kb * 1024,
    )
    app.state.batch_store.cleanup_expired()
    app.state.dataset_store = DatasetStore(app.state.project_store, app.state.batch_store)
    app.state.dataset_store.region_counter = count_regions
    app.state.region_service = RegionService(app.state.dataset_store)
    app.state.class_usage_counter = app.state.region_service.class_usage
    app.state.validation_service = ValidationService(
        app.state.project_store, app.state.dataset_store, app.state.region_service
    )
    app.state.job_manager = JobManager(
        settings.workspace.root,
        app.state.registry,
        app.state.dataset_store,
        app.state.region_service,
        max_workers=settings.runtime.maximum_concurrent_inference_jobs,
        max_queue=settings.runtime.maximum_queued_inference_jobs,
        event_recorder=app.state.project_store.record_event,
    )
    app.state.job_manager.recover_interrupted_jobs()
    app.state.export_service = ExportService(
        app.state.project_store,
        app.state.dataset_store,
        app.state.region_service,
        app.state.validation_service,
    )
    app.state.tswat_service = TswatImportService(
        settings.workspace.root,
        app.state.project_store,
        app.state.dataset_store,
        app.state.region_service,
    )

    install_error_handlers(app)
    app.add_middleware(AuthMiddleware, sessions=app.state.sessions, accounts=app.state.accounts)
    app.mount("/static", static_files(), name="static")
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(datasets_router)
    app.include_router(regions_router)
    app.include_router(validation_router)
    app.include_router(imports_router)
    app.include_router(segmentation_router)
    app.include_router(exports_router)
    app.include_router(help_router)
    app.include_router(pages_router)

    logger.info(
        "AFMLabel application created",
        extra={"version": __version__, "workspace": str(settings.workspace.root)},
    )
    return app


# ------------------------------------------------------------------------ CLI


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings(args.config)
    host = args.host or settings.server.host
    port = args.port or settings.server.port
    fastapi_app = create_app(settings)
    uvicorn.run(fastapi_app, host=host, port=port, log_config=None)
    return 0


def _version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def _auth_set_password(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    store = AccountStore(settings.workspace.root)
    password = os.environ.get("AFMLABEL_PASSWORD")
    if password:
        register_secret(password)
    else:
        if not sys.stdin.isatty():
            print(
                "no terminal available; set AFMLABEL_PASSWORD in the environment instead",
                file=sys.stderr,
            )
            return 2
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Repeat password: ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            return 2
        register_secret(password)
    try:
        account = store.set_password(args.username, password)
    except (WeakPasswordError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Any existing sessions belong to the old credential; drop them.
    SessionStore(settings.workspace.root).revoke_all()
    print(f"account {account.username!r} updated at {store.path}")
    return 0


def _auth_status(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    account = AccountStore(settings.workspace.root).load()
    if account is None:
        print("no account configured; run: afmlabel auth set-password --username <name>")
        return 1
    sessions = SessionStore(settings.workspace.root)
    print(f"username: {account.username}")
    print(f"algorithm: {account.algorithm}")
    print(f"updated_at: {account.updated_at}")
    print(f"active sessions: {sessions.active_count()}")
    return 0


def _auth_revoke_sessions(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    count = SessionStore(settings.workspace.root).revoke_all()
    print(f"revoked {count} session(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afmlabel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the AFMLabel FastAPI application")
    serve_parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=None, help="port (default 9080)")
    serve_parser.add_argument("--config", type=Path, default=None, help="path to config/app.yaml")
    serve_parser.set_defaults(func=_serve)

    version_parser = subparsers.add_parser("version", help="print the package version")
    version_parser.set_defaults(func=_version)

    auth_parser = subparsers.add_parser("auth", help="manage the single application account")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)
    set_pw = auth_sub.add_parser(
        "set-password",
        help="create the account or change its password (prompts or AFMLABEL_PASSWORD)",
    )
    set_pw.add_argument("--username", default="afmlabel")
    set_pw.add_argument("--config", type=Path, default=None)
    set_pw.set_defaults(func=_auth_set_password)
    status = auth_sub.add_parser("status", help="show the configured account")
    status.add_argument("--config", type=Path, default=None)
    status.set_defaults(func=_auth_status)
    revoke = auth_sub.add_parser("revoke-sessions", help="log out every browser session")
    revoke.add_argument("--config", type=Path, default=None)
    revoke.set_defaults(func=_auth_revoke_sessions)
    return parser


def cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(cli())
