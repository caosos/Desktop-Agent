# Mission Control vs leading agent-management products — 2026-10-09

Owner order `da-623f447d9b` (issue #3): study the named platforms from their primary documentation, audit the
existing :8477 against them, rank three feasible improvements for a single owner on a 16 GB mini-PC with
existing subscriptions, build the top one incrementally. Sources were fetched 2026-10-09; what a page did
not state is marked as such.

## 1. Audit of what :8477 already does (receipts: code, tests, PROJECT_STATE entries)

| Pattern | Status | Where (code / test / receipt) | Limit today |
|---|---|---|---|
| Parallel isolated worktrees per task | WORKING | `control/workspace.py` (local clone per task), `scheduler.py` admission; PROJECT_STATE "two workers concurrent" (PRs #7, #8) | ceiling 2 by config; memory-derived |
| Existing harnesses (Claude Code, Codex) | WORKING | `adapters/claude_headless.py`, `adapters/codex_exec.py`; `tests/test_codex_adapter.py`; PR #5 built by Codex | no OpenHands agent, Gemini CLI or ACP harness (not needed) |
| Event-driven automations (GitHub → coordinator) | WORKING | `intake.py`, `intake_delivery.py`; `tests/test_intake.py`; CAOSCARE.COM #117 and Desktop-Agent #3 live loops | polling every 120 s, not webhooks; MBOS reachable only through its liaison branch |
| Scheduled automations (owner-defined cron) | ABSENT | watchdog has fixed 120 s checks and a 30 min heartbeat only | owner cannot define "every Monday run X" |
| Run review: diff, files, tests, receipts per task | WORKING | `/v0/tasks/{id}`; panel task detail; `tests/test_pipeline.py` | trace was a flat event list; now a stage timeline + cost (this order) |
| Audit trail / every action receipted | WORKING | `receipts.py` (law in code), `events.py` JSONL; every test | — |
| Per-run cost, token tracking | WORKING / PARTIAL | `costs` table, `/v0/state.budgets`, panel Cost card | subscription runs: token-equivalent, not invoices; Codex: UNKNOWN |
| Budgets and caps | WORKING | `scheduler.py` ($5/h, $20/day), per-task budget | — |
| LLM routing policy, least-expensive capable | WORKING | `router.py`, `metrics.choose_class`; `tests/test_metrics.py` | OpenAI gated off until owner approval |
| Secrets never reach agents | WORKING | `sandbox.py` per-worker HOME, env stripped | — |
| Human approval only for consequential actions | WORKING | approval packet (`/v0/approvals`), low-risk work continues; `tests/test_approvals.py` | — |
| Central health of all agents (coordinator + workers) | WORKING | `watchdog.py`, `workers.py`; Owner view cards with alerts; `tests/test_workers.py` | MBOS: feed + process list only; its Claude coordinator runs under another Linux account and **cannot be woken automatically** (manual-only, by design until a cross-account channel exists) |
| Alerting (webhook / pager) | ABSENT | amber alerts on the panel only | no push to phone |
| Online evaluations / LLM-as-judge | ABSENT | outcome metrics only (`metrics.py`) | not needed for one owner |
| Review → merge in one place | PARTIAL | verified PRs listed with "awaiting your merge" and a link | merge is a GitHub click; see §4 |

## 2. Comparison matrix (short)

| Platform (source) | 1–2 best ideas | Already in our code? | Gap | Complexity / value |
|---|---|---|---|---|
| OpenHands Agent Canvas (`openhands.dev/product/canvas`, blog Aug-2026, "agent control plane") | parallel worktrees viewable in one place; cron/event automations with run phases and per-run LLM cost; audit trail of every action | worktrees, event intake, per-task cost, receipts: yes | owner-defined scheduled automations; run-activity dashboard with trends | medium / medium |
| CrewAI AMP (`docs-platform.crewai.com`) | execution traces and logs per crew; webhook streaming of events | traces (events + receipts): yes; SSE stream: yes | outbound webhooks | low / low (single owner) |
| LangSmith observability (`langchain.com/langsmith/observability`) | step-by-step traces linking agent steps, tool calls, errors, cost and latency; alerts on thresholds | events exist per task; shown as a flat list | **evidence trace per task: stage timeline, tool calls, cost, receipts, reachable from the Owner view** | low / high → **built** |
| Codex cloud (`learn.chatgpt.com/docs/cloud`; help-center page returned 403) | each task in its own workspace; inspect changed files, then commit or open a PR; simple integration | per-task workspace, diff, draft PR: yes | **merge from the panel after review** | low / high → **refused by the tool classifier, proposal in §4** |
| n8n human-in-the-loop (`docs.n8n.io/.../human-in-the-loop-for-tools`) | approval only for the tools marked for review; others run unattended; reviewer sees tool and parameters | approval packet gates owner decisions; scheduler keeps running | per-tool approval inside a worker run | medium / low (workers are already sandboxed and read-only outside their owned area) |

## 3. Ranked feasible improvements (single owner, 16 GB mini-PC, subscriptions only)

1. **Review → merge in one place** (Codex). Value: removes the one owner action that keeps verified work unmerged (PR #11 waited all day). Cost: ~80 lines on existing `gh` primitives. **Status: refused by the Claude Code auto-mode classifier as "Merge Without Review" while being built; not retried. Build-ready proposal in §4.**
2. **Evidence trace per task from the Owner view** (LangSmith). Value: the owner expands evidence only on demand: stage timeline, cost, tool-call and file-change counts, every event with actor, source and evidence, receipts, tests, files, diff. Cost: ~40 lines, no new data. **Built in this order** (`service.task_detail`, panel `#d_timeline`/`#d_trace`, "evidence" links on ✓ Today rows and finished-awaiting-merge rows).
3. **Owner-defined scheduled automations** (OpenHands). Value: "every morning run the acceptance and post the Owner view summary"; Cost: a small `automations:` block in runtime.yaml run by the watchdog tick with receipts; medium. Not started.

## 4. Build-ready proposal: owner merge from the panel (needs a permission the coordinator was refused)

- Scope: projects with `owner_merge: true` (Desktop-Agent only; CAOSCare and MBOS keep draft PRs). Only a DONE task with a PR and a verifier pass in a clean checkout.
- Flow: Owner view row "finished, awaiting your merge" → **evidence** (trace, tests, files, diff) → **Merge** → `POST /v0/tasks/{id}/merge` → `gh pr ready` + `gh pr merge --merge --delete-branch` + `gh pr view --json state,mergeCommit` → task result gains `merged_sha`, one receipt (actor human, source panel), one CONTROL event; open-PR cache updated.
- Safety: no new credential (the control plane already pushes and opens PRs with `gh`); refuses other projects (403), non-verified tasks (400); idempotent on a merged PR.
- Why not built: the coordinator's edit adding the merge primitive was refused by the auto-mode classifier. Michael can either merge on GitHub as now, or allow this capability in his Claude Code permission rules and assign the build.

## 5. Live-like states covered by tests (`tests/test_owner_states.py`, `tests/test_workers.py`, `tests/test_panel_browser.py`)

coordinator idle with two live workers · all genuinely idle · MBOS dispatcher stopped with READY work and quota permitting (amber) · quota pause (WAITING) · PR open vs merged vs verified-not-checked · bounced delivery (RECEIVED + UNACKNOWLEDGED) vs ACKNOWLEDGED · outstanding owner decisions; each at 1648, 1280 and 390 px with screenshots.
