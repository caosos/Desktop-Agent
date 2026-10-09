"""Provider configuration, key loading, OpenAI backend (with a fake HTTP layer), pricing and budgets.
No network, no paid calls."""
import asyncio
import json
from pathlib import Path

import pytest

from desktop_agent.control import llm as llm_mod
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.llm import LLM, LLMError, Completion, list_openai_models
from desktop_agent.control.service import Service


def _cfg(tmp_path: Path, yaml_text: str) -> RuntimeConfig:
    p = tmp_path / "runtime.yaml"; p.write_text(yaml_text)
    return RuntimeConfig.load(p)


def test_key_from_file_and_status(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    kf = tmp_path / "openai.key"
    cfg = _cfg(tmp_path, f"providers:\n  openai:\n    key_file: {kf}\n    models: {{cloud_cheap: m-luna, cloud_strong: m-sol, cloud_max: m-astra}}\n"
                         "llm_backend_order: [openai_api, claude_cli]\npricing:\n  m-luna: {in: 0.5, out: 2.0}\n")
    st = cfg.provider_status("openai")
    assert st["key_present"] is False and st["configured"] is True
    assert LLM(cfg).backend == "claude_cli"                      # no key yet → subscription fallback
    kf.write_text("sk-test-not-a-real-key\n"); kf.chmod(0o600)
    assert cfg.provider_key("openai") == "sk-test-not-a-real-key"
    st = cfg.provider_status("openai")
    assert st["key_present"] and st["key_file_mode"] == "0o600"
    assert LLM(cfg).backend == "openai_api"
    assert cfg.price("m-luna", 1_000_000, 100_000) == 0.5 + 0.2 and cfg.price("unpriced", 10, 10) is None


def test_openai_backend_parses_and_prices(tmp_path: Path, monkeypatch):
    kf = tmp_path / "openai.key"; kf.write_text("sk-test");
    cfg = _cfg(tmp_path, f"providers:\n  openai:\n    key_file: {kf}\n    models: {{cloud_cheap: m-luna, cloud_strong: m-sol, cloud_max: m-astra}}\n"
                         "llm_backend_order: [openai_api]\npricing:\n  m-luna: {in: 1.0, out: 4.0}\n")
    seen = {}
    def fake_post(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers)
        return {"model": "m-luna", "choices": [{"message": {"content": json.dumps({"task_type": "docs"})}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
    monkeypatch.setattr(llm_mod, "_post_json", fake_post)
    c = asyncio.run(LLM(cfg).complete("classify", {"type": "object"}, "cloud_cheap", system="sys"))
    assert c.backend == "openai_api" and c.data == {"task_type": "docs"} and c.model == "m-luna"
    assert c.cost_usd == pytest.approx(0.001 + 0.0004) and c.cost_known
    assert seen["headers"]["authorization"] == "Bearer sk-test" and seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    # unpriced model → unknown cost, not an estimate
    cfg.pricing = {}
    c2 = asyncio.run(LLM(cfg).complete("x", {"type": "object"}, "cloud_strong"))
    assert c2.cost_usd is None and not c2.cost_known and c2.model == "m-luna"   # fake returns m-luna


def test_openai_http_error_surfaces(tmp_path: Path, monkeypatch):
    kf = tmp_path / "openai.key"; kf.write_text("sk-test")
    cfg = _cfg(tmp_path, f"providers:\n  openai:\n    key_file: {kf}\n    models: {{cloud_cheap: m, cloud_strong: m, cloud_max: m}}\nllm_backend_order: [openai_api]\n")
    def boom(url, body, headers, timeout): raise LLMError("api.openai.com HTTP 429: insufficient_quota")
    monkeypatch.setattr(llm_mod, "_post_json", boom)
    with pytest.raises(LLMError, match="429"):
        asyncio.run(LLM(cfg).complete("x", {"type": "object"}))
    with pytest.raises(LLMError, match="no key"):
        cfg.providers["openai"]["key_file"] = str(tmp_path / "missing"); list_openai_models(cfg)


def test_budgets_and_unknown_usage_in_state(tmp_path: Path):
    repo = tmp_path / "alpha"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    y = tmp_path / "a.yaml"; y.write_text(f"name: alpha\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n")
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[y])
    s = Service(cfg); s.scheduler.paused = True
    s.store.add_cost("t1", "w", "claude-sonnet-5-5", 1.25, 100, 50)
    s.store.add_cost("t2", "w", "gpt-x", None, 2000, 300)              # subscription / unpriced
    b = s.state()["budgets"]
    assert b["spent_today_usd"] == 1.25 and b["remaining_today_usd"] == round(cfg.scheduler.daily_cap_usd - 1.25, 4)
    assert b["unknown_usage"]["calls"] == 1 and b["unknown_usage"]["input_tokens"] == 2000
    assert s.state()["llm"]["backend"] in ("claude_cli", "openai_api", "anthropic_api")
    assert s.state()["costs"]["per_task"] == {"t1": 1.25}
