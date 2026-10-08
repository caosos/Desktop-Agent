"""class_labels: defaults, yaml override, unknown-key rejection, exposure in /v0/state."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.service import Service

DEFAULTS = {"cloud_cheap": "Luna-class", "cloud_strong": "Sol-class", "cloud_max": "Astra-class"}


def _load(tmp_path: Path, text: str) -> RuntimeConfig:
    f = tmp_path / "runtime.yaml"
    f.write_text(text)
    return RuntimeConfig.load(f)


def test_defaults_without_yaml_entry(tmp_path: Path):
    assert _load(tmp_path, "api: {port: 1}\n").class_labels == DEFAULTS


def test_yaml_overrides_one_label(tmp_path: Path):
    cfg = _load(tmp_path, "class_labels:\n  cloud_strong: Terra-class\n")
    assert cfg.class_labels == DEFAULTS | {"cloud_strong": "Terra-class"}


def test_unknown_class_key_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="cloud_bogus"):
        _load(tmp_path, "class_labels:\n  cloud_bogus: X\n")


def test_state_exposes_class_labels(tmp_path: Path):
    cfg = _load(tmp_path, f"data_dir: {tmp_path / 'data'}\nclass_labels:\n  cloud_max: Nova-class\n")
    s = Service(cfg)
    s.scheduler.paused = True
    with TestClient(build_app(s, "tok")) as c:
        st = c.get("/v0/state", headers={"Authorization": "Bearer tok"}).json()
    assert st["class_labels"] == DEFAULTS | {"cloud_max": "Nova-class"}
    assert set(st["class_labels"]) == set(DEFAULTS)
