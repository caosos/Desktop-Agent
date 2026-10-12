"""The one hard daily budget (owner directive da-8708eb5ec7): $0 default, paid calls blocked while subscription work
continues, one NEED receipt and no charge for a refused call, no subscription figure called spend, restart and replay
safe, no bypass through the relay or workers. No network: the paid HTTP layer is a fake."""
import asyncio
import json
import threading
from pathlib import Path

import pytest

from desktop_agent.control import llm as llm_mod
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.llm import LLM, Completion, LLMError, subscription_env
from desktop_agent.control.paid_budget import DEFAULT_MAX_USD, HOLD_USD, PaidBudget
from desktop_agent.control.store import Store


def cfg_with_openai(tmp_path: Path) -> RuntimeConfig:
    kf = tmp_path / "k"; kf.write_text("sk-test"); kf.chmod(0o600)
    p = tmp_path / "runtime.yaml"
    p.write_text(f"providers:\n  openai:\n    key_file: {kf}\n    models: {{cloud_cheap: m-luna, cloud_strong: m-sol, cloud_max: m-astra}}\n"
                 "llm_backend_order: [openai_api, claude_cli]\npricing:\n  m-luna: {in: 1.0, out: 4.0}\n")
    return RuntimeConfig.load(p)


class Rig:
    def __init__(self, tmp_path: Path, maximum: float | None = None):
        self.dir = tmp_path
        self.cfg = cfg_with_openai(tmp_path)
        self.open(); self.paid_posts = 0; self.cli_calls = 0
        if maximum is not None:
            self.pb.set_maximum(maximum, "test")
        def post(url, body, headers, timeout):
            self.paid_posts += 1
            return {"model": "m-luna", "choices": [{"message": {"content": json.dumps({"ok": 1})}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
        llm_mod._post_json = post
        async def cli(prompt, schema, model, system, timeout):
            self.cli_calls += 1
            return Completion(data={"ok": 1}, model=model, backend="claude_cli", cost_usd=3.77, input_tokens=9, output_tokens=9, duration_ms=1)   # a big token-equivalent figure
        self.llm._claude_cli = cli

    def open(self):
        """(Re)open the store over the same directory: what a service restart does."""
        self.store = Store(self.dir / "data"); self.pb = PaidBudget(self.store)
        self.llm = LLM(self.cfg); self.llm.gate = self.pb
        if hasattr(self, "cli_calls"):
            async def cli(prompt, schema, model, system, timeout):
                self.cli_calls += 1
                return Completion(data={"ok": 1}, model=model, backend="claude_cli", cost_usd=3.77, input_tokens=9, output_tokens=9, duration_ms=1)
            self.llm._claude_cli = cli

    def call(self, purpose="test task"):
        return asyncio.run(self.llm.complete("p", {"type": "object"}, "cloud_cheap", purpose=purpose))

    def needs(self):
        return [r for r in self.store.receipts("budget", self.pb.day()) if r["claim"].startswith("NEED_OWNER")]


def test_default_maximum_is_zero_in_code_and_state(tmp_path):
    assert DEFAULT_MAX_USD == 0.0
    r = Rig(tmp_path)
    assert r.pb.maximum() == 0.0 and r.pb.summary()["daily_max_paid_usd"] == 0.0 and r.pb.permits()[0] is False


def test_zero_maximum_blocks_paid_calls_and_subscription_work_continues_with_one_need_receipt(tmp_path):
    r = Rig(tmp_path)
    c = r.call("Ask Aria conversation")
    assert c.backend == "claude_cli" and r.cli_calls == 1 and r.paid_posts == 0                    # subscription route ran, no paid HTTP request
    assert r.store.paid_spent(r.pb.day()) == 0.0 and r.store.paid_rows(r.pb.day()) == []          # no charge, no hold
    needs = r.needs()
    assert len(needs) == 1 and "provider=openai" in needs[0]["evidence"] and "purpose=Ask Aria conversation" in needs[0]["evidence"]
    assert needs[0]["after_state"]["charged_usd"] == 0.0 and needs[0]["after_state"]["blocks_work"] is False
    assert "NEED_OWNER" in needs[0]["claim"] and "$0.10" in needs[0]["claim"] and needs[0]["actor"] == "control" and needs[0]["evidence"]
    r.call("Ask Aria conversation"); r.call("Ask Aria conversation")
    assert len(r.needs()) == 1 and r.pb.summary()["blocked_attempts_today"] == 3 and r.cli_calls == 3       # one receipt per day/provider/purpose, every call still counted


def test_subscription_usage_is_never_paid_spend(tmp_path):
    r = Rig(tmp_path)
    for _ in range(5):
        r.call()
    s = r.pb.summary()
    assert s["paid_today_usd"] == 0.0 and s["paid_calls_today"] == 0 and s["paid_today_verified_usd"] == 0.0
    assert r.store.paid_rows(r.pb.day()) == []                      # a $3.77 token-equivalent figure per call never reached the ledger
    assert not any(k in json.dumps(s).lower() for k in ("token", "hourly", "spent_", "daily_cap"))


def test_with_a_maximum_the_paid_route_runs_is_held_then_settled_and_stops_at_the_limit(tmp_path):
    r = Rig(tmp_path, maximum=0.25)
    c = r.call()
    assert c.backend == "openai_api" and r.paid_posts == 1 and r.cli_calls == 0
    rows = r.store.paid_rows(r.pb.day())
    assert len(rows) == 1 and rows[0]["status"] == "settled" and rows[0]["basis"] == "provider_reported" and rows[0]["usd"] == pytest.approx(0.0014)
    r.call(); r.call()                                             # 3 calls still fit: 0.0014 each reported
    assert r.paid_posts == 3
    # unknown-price model keeps the conservative hold, so the maximum is reached by holds, never exceeded
    r.cfg.pricing = {}
    for _ in range(5):
        r.call()
    assert r.store.paid_spent(r.pb.day()) <= 0.25 + 1e-9 and r.paid_posts < 8 and r.cli_calls >= 1
    assert r.needs() and r.needs()[0]["after_state"]["blocks_work"] is False


def test_concurrent_calls_cannot_jointly_exceed_the_maximum(tmp_path):
    r = Rig(tmp_path, maximum=HOLD_USD * 2)                        # room for exactly two holds
    results = []
    def reserve(i): results.append(r.pb.authorize(f"c{i}", "openai", "m", "t")["ok"])
    ts = [threading.Thread(target=reserve, args=(i,)) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count(True) == 2 and r.store.paid_spent(r.pb.day()) == pytest.approx(HOLD_USD * 2)


def test_restart_and_replay_keep_the_ledger_and_the_maximum(tmp_path):
    r = Rig(tmp_path, maximum=HOLD_USD)
    assert r.pb.authorize("call-1", "openai", "m", "t")["ok"]
    r.open()                                                       # service restart over the same data directory
    assert r.pb.maximum() == HOLD_USD and r.store.paid_spent(r.pb.day()) == HOLD_USD
    assert r.pb.authorize("call-1", "openai", "m", "t")["replay"] is True                 # the same call id again: nothing added
    assert r.store.paid_spent(r.pb.day()) == HOLD_USD and len(r.store.paid_rows(r.pb.day())) == 1
    assert r.pb.authorize("call-2", "openai", "m", "t")["ok"] is False                    # the restart did not reset the day
    assert r.call().backend == "claude_cli" and r.paid_posts == 0


def test_a_paid_http_call_without_a_ledger_hold_is_refused(tmp_path):
    r = Rig(tmp_path, maximum=5)
    with pytest.raises(LLMError, match="no daily-maximum hold"):
        r.llm._openai("p", {"type": "object"}, "m-luna", None, 5, "never-reserved")
    with pytest.raises(LLMError, match="no daily-maximum hold"):
        r.llm._anthropic("p", {"type": "object"}, "m", None, 5, "never-reserved")
    nogate = LLM(r.cfg)                                            # no ledger attached: fail closed
    assert nogate.backend_for("cloud_cheap") == "claude_cli" and r.paid_posts == 0


def test_only_the_owner_setting_changes_the_maximum_and_each_change_is_receipted(tmp_path):
    r = Rig(tmp_path)
    rec = r.pb.set_maximum(2.5, "panel")
    assert r.pb.maximum() == 2.5 and rec["actor"] == "human" and rec["before_state"] == {"daily_max_usd": 0.0} and rec["after_state"] == {"daily_max_usd": 2.5}
    for bad in (-1, float("nan"), 10001):
        with pytest.raises(ValueError):
            r.pb.set_maximum(bad)


def test_billing_receipts_are_the_authority_and_are_idempotent(tmp_path):
    r = Rig(tmp_path, maximum=5)
    r.call()
    ids = [x["call_id"] for x in r.store.paid_rows(r.pb.day())]
    assert r.pb.record_billing("openai", 0.0021, "inv-1", covers=ids) is not None
    assert r.pb.record_billing("openai", 0.0021, "inv-1", covers=ids) is None                # replay: nothing added
    s = r.pb.summary()
    assert s["paid_today_usd"] == pytest.approx(0.0021) and s["paid_today_verified_usd"] == pytest.approx(0.0021)   # covered rows no longer count twice


def test_every_claude_cli_the_control_plane_starts_has_no_paid_credentials(monkeypatch):
    env = subscription_env({"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "http://x",
                            "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/bin", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}
    import inspect
    from desktop_agent.control import intake_delivery
    assert "subscription_env(os.environ)" in inspect.getsource(intake_delivery) and "subscription_env(os.environ)" in inspect.getsource(llm_mod)


def test_api_and_state_expose_one_setting_and_no_spend_words(tmp_path):
    from fastapi.testclient import TestClient
    from desktop_agent.control.api import build_app
    from desktop_agent.control.service import Service
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t")
    s = Service(cfg); app = build_app(s, "tok")
    h = {"Authorization": "Bearer tok"}
    c = TestClient(app)
    assert c.get("/v0/paid-budget").status_code == 401
    st = c.get("/v0/state", headers=h).json()
    assert st["budgets"]["daily_max_paid_usd"] == 0.0 and "costs" not in st and st["estimates"]["label"] == "estimate; not billed"
    assert c.post("/v0/paid-budget", json={"daily_max_usd": -2}, headers=h).status_code == 422
    r = c.post("/v0/paid-budget", json={"daily_max_usd": 3}, headers=h).json()
    assert r["daily_max_paid_usd"] == 3.0 and c.get("/v0/state", headers=h).json()["budgets"]["daily_max_paid_usd"] == 3.0
    b = c.post("/v0/paid-budget/billing", json={"provider": "openai", "usd": 0.5, "reference": "inv-9"}, headers=h).json()
    assert b["recorded"] and b["paid_today_verified_usd"] == 0.5 and c.post("/v0/paid-budget/billing", json={"provider": "openai", "usd": 0.5, "reference": "inv-9"}, headers=h).json()["duplicate"]


def test_ask_aria_never_quotes_subscription_usage_as_spend_cost_or_cap(tmp_path):
    """Found live: with old replies in the conversation ("$5.11 of the $20 daily cap") the model repeated them. The reply guard and the
    prompt scrub keep that wording out of both the answer and the model's context."""
    from desktop_agent.control.aria_usage import money_statement, quotes_obsolete_money
    from tests.test_aria_brain import FakeLLM, _service
    for bad in ("The control plane records $5.11 of paid API spend today against your $20 daily cap.", "Spend today is $5.11 of the $20 daily cap.", "It cost about $0.12 per message.", "The hourly cap is $5."):
        assert quotes_obsolete_money(bad)
    for ok in (money_statement(0.0, 0.0), "Paid API today $0.00 of your $2.00 daily maximum.", "Nothing is running.", "It took 3 rounds."):
        assert not quotes_obsolete_money(ok)
    s = _service(tmp_path)
    s.aria.history = [{"role": "owner", "text": "cost?", "ts": 1}, {"role": "aria", "text": "Spend is $5.09 of the $20 daily cap.", "ts": 2, "kind": "question"}]
    s.aria.llm = FakeLLM([{"reply": "You have spent $5.11 against the $20 daily cap.", "action": None}])
    r = asyncio.run(s.aria.chat("what did that cost, and what is my budget?"))
    assert "$5" not in r["reply"] and "$20" not in r["reply"] and "Daily maximum paid API spend is $0.00" in r["reply"] and "included in your subscription" in r["reply"]
    assert "$5.09" not in s.aria.llm.prompts[0] and "withdrawn" in s.aria.llm.prompts[0]


def test_the_visible_transcript_withdraws_old_dollar_replies_but_the_store_keeps_them(tmp_path):
    from tests.test_aria_brain import _service
    s = _service(tmp_path)
    s.aria.history = [{"role": "owner", "text": "cost?", "ts": 1}, {"role": "aria", "text": "Spend is $5.09 of the $20 daily cap.", "ts": 2, "kind": "question"}]
    assert "$" not in " ".join(t["text"] for t in s.aria.transcript()) and "$5.09" in s.aria.history[1]["text"]
