# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Static application configuration.

Loaded from ``config/app.yaml`` (see ``config/app.example.yaml``) with
environment-variable overrides (``AFMLABEL_`` prefix, ``__`` nested
delimiter). Only static, administrator-edited configuration lives here;
mutable project state is modelled in the ``domain``/``persistence``
packages and is never read from YAML.

Secrets (``HF_TOKEN``, the account password, the session secret) are
deliberately *not* fields of these models so they can never be written into
``config/app.yaml`` by mistake.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/app.yaml")
CONFIG_PATH_ENV_VAR = "AFMLABEL_CONFIG_FILE"

BoundaryMode = Literal["disabled", "explicit_class", "fill_unlabeled_after_approval"]


class WorkspaceSettings(BaseModel):
    root: Path = Path("./workspace")
    minimum_free_disk_gb: float = 10.0


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9080


class RuntimeSettings(BaseModel):
    #: auto | cpu | cuda | cuda:N | rocm
    device: str = "auto"
    maximum_concurrent_inference_jobs: int = 1
    #: Bounded queue depth for the local inference job manager.
    maximum_queued_inference_jobs: int = 8
    resource_refresh_seconds: float = 2.0
    default_backend: str = "fake"


class Sam31ModelSettings(BaseModel):
    enabled: bool = False
    repository: str = "facebook/sam3.1"
    #: Exact Hugging Face commit hash; ``None`` means "not pinned yet".
    revision: str | None = None
    checkpoint_dir: Path = Path("./models/sam31")
    #: Exact git revision of the official SAM 3 code the checkpoint was
    #: validated against.
    code_revision: str | None = None
    allow_cpu: bool = False
    require_startup_diagnostic: bool = False

    @field_validator("revision", "code_revision")
    @classmethod
    def _reject_moving_revisions(cls, value: str | None) -> str | None:
        if value is not None and value.strip().lower() in {"main", "master", "latest", "head"}:
            raise ValueError(
                f"{value!r} is a moving reference; pin an exact commit hash instead "
                "(reproducibility over following a moving branch)"
            )
        return value


class Sam3ModelSettings(Sam31ModelSettings):
    """SAM 3 image checkpoint (``facebook/sam3``, ``sam3.pt``): point, box,
    mask and text prompts plus multi-mask proposals through the pinned
    official code. Same fields as :class:`Sam31ModelSettings`."""

    repository: str = "facebook/sam3"
    checkpoint_dir: Path = Path("./models/sam3")


class Sam21ModelSettings(Sam31ModelSettings):
    """SAM 2.1 through the Hugging Face ``transformers`` port
    (``facebook/sam2.1-hiera-base-plus``): small, CPU-capable, point/box/mask
    prompts and multi-mask proposals, no text prompts. ``code_revision`` is
    the ``transformers`` version recorded at download time."""

    repository: str = "facebook/sam2.1-hiera-base-plus"
    checkpoint_dir: Path = Path("./models/sam21")
    allow_cpu: bool = True


class ModelsSettings(BaseModel):
    sam31: Sam31ModelSettings = Field(default_factory=Sam31ModelSettings)
    sam3: Sam3ModelSettings = Field(default_factory=Sam3ModelSettings)
    sam21: Sam21ModelSettings = Field(default_factory=Sam21ModelSettings)


class SecuritySettings(BaseModel):
    session_timeout_minutes: int = 120
    allow_external_runtime_network: bool = False
    #: Set ``Secure`` on the session cookie. Enable behind HTTPS.
    secure_cookies: bool = False
    #: Maximum failed login attempts per client within ``login_rate_window_seconds``.
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300


class AnnotationSettings(BaseModel):
    boundary_mode: BoundaryMode = "disabled"
    require_overlap_review: bool = True


class ResourceSettings(BaseModel):
    low_ram_warning_gb: float = 2.0
    low_disk_warning_gb: float = 10.0


class LoggingSettings(BaseModel):
    level: str = "INFO"
    json_output: bool = True
    #: Log the successful background polls the browser issues every few
    #: seconds (resource monitor, model list, lock heartbeat, job status).
    #: Off by default so the access log shows user actions, not heartbeats.
    log_polling_requests: bool = False


class ExportSettings(BaseModel):
    #: Profile preselected by the export dialog and used when a request omits
    #: ``profile_id``.
    default_profile: str = "nanounet"


class UploadSettings(BaseModel):
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".tif", ".tiff", ".png", ".jpg", ".jpeg"]
    )
    maximum_file_size_mb: float = 512.0
    chunk_size_kb: int = 512


class DeveloperSettings(BaseModel):
    """Developer-local references. Never commit machine-specific values;
    set ``AFMLABEL_DEVELOPER__LABVIST_REFERENCE_PATH`` in the environment
    or ``config/developer.local.yaml`` (git-ignored) instead."""

    labvist_reference_path: Path | None = None


class AppSettings(BaseSettings):
    """Root application settings.

    Values are layered: ``config/app.yaml`` provides the base, environment
    variables (prefix ``AFMLABEL_``, ``__`` nested delimiter) always win
    over the file.
    """

    model_config = SettingsConfigDict(
        env_prefix="AFMLABEL_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    schema_version: int = 1
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    annotation: AnnotationSettings = Field(default_factory=AnnotationSettings)
    resources: ResourceSettings = Field(default_factory=ResourceSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    developer: DeveloperSettings = Field(default_factory=DeveloperSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment variables must win over values loaded from the YAML
        # file (passed in as init kwargs), so they are listed first.
        return (env_settings, init_settings, dotenv_settings, file_secret_settings)


_FORBIDDEN_YAML_KEYS = frozenset({"hf_token", "password", "session_secret", "secret"})


def resolve_config_path(config_path: Path | None = None) -> Path:
    if config_path is not None:
        return config_path
    env_value = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_value:
        return Path(env_value)
    return DEFAULT_CONFIG_PATH


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    _reject_secret_keys(path, loaded, prefix="")
    return loaded


def _reject_secret_keys(path: Path, mapping: dict[str, Any], prefix: str) -> None:
    for key, value in mapping.items():
        dotted = f"{prefix}{key}"
        if str(key).lower() in _FORBIDDEN_YAML_KEYS:
            raise ValueError(
                f"{path}: key {dotted!r} looks like a secret; secrets are read from the "
                "environment only and must never be written to configuration files"
            )
        if isinstance(value, dict):
            _reject_secret_keys(path, value, prefix=f"{dotted}.")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(config_path: Path | None = None) -> AppSettings:
    """Load application settings from YAML plus environment overrides.

    A missing configuration file is not an error: a fresh checkout must boot
    with safe defaults (model backends disabled, localhost binding). A
    git-ignored ``config/developer.local.yaml`` next to the main file is
    merged on top for developer-local values such as the LabVIST path.
    """
    path = resolve_config_path(config_path)
    raw = _load_yaml_mapping(path)
    developer_local = path.parent / "developer.local.yaml"
    if developer_local.exists():
        raw = _deep_merge(raw, _load_yaml_mapping(developer_local))
    return AppSettings(**raw)
