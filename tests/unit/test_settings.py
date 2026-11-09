# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import pytest

from afmlabel.settings import AppSettings, load_settings


def test_defaults_are_safe_for_a_fresh_checkout(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.server.host == "127.0.0.1"
    assert settings.models.sam31.enabled is False
    assert settings.security.allow_external_runtime_network is False
    assert settings.annotation.boundary_mode == "disabled"
    assert settings.runtime.default_backend == "fake"
    # NanoUNet is the default export profile.
    assert settings.export.default_profile == "nanounet"


def test_shipped_example_config_keeps_the_nanounet_default() -> None:
    """A fresh ``cp config/app.example.yaml config/app.yaml`` must not
    silently downgrade the export default. Installations
    carrying an app.yaml from 0.1.0 keep their own value; the upgrade notes
    tell operators to update it."""
    example = Path(__file__).resolve().parents[2] / "config" / "app.example.yaml"
    settings = load_settings(example)
    assert settings.export.default_profile == "nanounet"


def test_yaml_values_are_loaded(tmp_path: Path) -> None:
    config = tmp_path / "app.yaml"
    config.write_text("server:\n  port: 9999\nannotation:\n  boundary_mode: explicit_class\n")
    settings = load_settings(config)
    assert settings.server.port == 9999
    assert settings.annotation.boundary_mode == "explicit_class"


def test_environment_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "app.yaml"
    config.write_text("server:\n  port: 9999\n")
    monkeypatch.setenv("AFMLABEL_SERVER__PORT", "7070")
    monkeypatch.setenv("AFMLABEL_WORKSPACE__ROOT", str(tmp_path / "ws"))
    settings = load_settings(config)
    assert settings.server.port == 7070
    assert settings.workspace.root == tmp_path / "ws"


def test_developer_local_yaml_is_merged(tmp_path: Path) -> None:
    (tmp_path / "app.yaml").write_text("server:\n  port: 9999\n")
    (tmp_path / "developer.local.yaml").write_text(
        "developer:\n  labvist_reference_path: /somewhere/labvist\n"
    )
    settings = load_settings(tmp_path / "app.yaml")
    assert settings.server.port == 9999
    assert settings.developer.labvist_reference_path == Path("/somewhere/labvist")


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        AppSettings(unknown_section={"x": 1})


def test_secret_keys_in_yaml_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "app.yaml"
    config.write_text("models:\n  sam31:\n    hf_token: hf_abc\n")
    with pytest.raises(ValueError, match="secret"):
        load_settings(config)


@pytest.mark.parametrize("value", ["main", "latest", "HEAD"])
def test_moving_model_revisions_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="moving reference"):
        AppSettings(models={"sam31": {"revision": value}})


def test_invalid_boundary_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        AppSettings(annotation={"boundary_mode": "guess"})
