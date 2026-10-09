"""The real descriptors under config/ must load: the service refuses to start on a YAML mistake, which the
fixture-based tests would never see."""
from pathlib import Path

import yaml

from desktop_agent.control.project import ProjectPackage

ROOT = Path(__file__).resolve().parents[1]


def test_real_project_descriptors_load():
    names = []
    for f in sorted((ROOT / "config" / "projects").glob("*.yaml")):
        pkg = ProjectPackage.load(f)
        names.append(pkg.name)
        assert pkg.mission                                           # present; loading proves the quoting
        assert pkg.workers is not None, f"{f.name}: every project declares its worker telemetry or lack of it"
    assert names == ["caoscare", "desktop_agent", "michael_business_os"]


def test_runtime_yaml_loads_and_keeps_openai_gate_off():
    raw = yaml.safe_load((ROOT / "config" / "runtime.yaml").read_text())
    assert raw["providers"]["openai"]["enabled"] is False                 # owner gate: never flipped by code
