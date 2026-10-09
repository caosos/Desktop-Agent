# Desktop-Agent — system blueprint

One linked map of the whole system. It points at the canonical sources and
at the files that implement each part; it does not restate them. If this
file and a source disagree, the source wins and this file is the defect.

Canonical sources (read these, do not copy them):

| Source | Holds |
|---|---|
| [`AGENTS.md`](../AGENTS.md) | working rules, receipt law, stop conditions, handoff |
| [`START_HERE.md`](../START_HERE.md) | bootloader, reading order, host facts, how to run |
| [`docs/OWNER_DIRECTIVES.md`](OWNER_DIRECTIVES.md) | Michael's direction, dated (linked here, never restated) |
| [`docs/IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | system shape, widget, API, scheduler, stages |
| [`docs/ARCHITECTURE_REVIEW_2026-10-08.md`](ARCHITECTURE_REVIEW_2026-10-08.md) | reviewed architecture: reuse, sandbox, event/receipt model |
| [`docs/PROJECT_STATE.md`](PROJECT_STATE.md) | dated, append-only build state (written by the control plane for tasks) |
| [`docs/WIDGET_SETUP.md`](WIDGET_SETUP.md) | running the widget in Michael's session |

## Status labels

Every component below carries exactly one label and one citation.

- **VERIFIED-IMPLEMENTED** — the code exists and is exercised by the cited
  test file and/or recorded in the cited `PROJECT_STATE.md` entry (entry
  titles are quoted so they can be found; PROJECT_STATE is append-only).
- **PROPOSED** — described in `IMPLEMENTATION_PLAN.md` or the review, not
  built (or deliberately deferred). Cited by section.
- **HOST-UNVERIFIED** — code exists but has not been exercised on this host,
  or was exercised only through fakes; the citation says what is missing.

A label is a claim about evidence, not about quality. "Tests pass" for a
component means the cited tests pass in the suite
(`.venv/bin/python -m pytest -q`, no network, no model calls); see
`AGENTS.md` for what counts as proof for a worker's own claims.

---

## 1. Architecture and ownership boundaries

Shape and diagram: [`IMPLEMENTATION_PLAN.md` §1](IMPLEMENTATION_PLAN.md).
Rationale and what not to build: review
[§1](ARCHITECTURE_REVIEW_2026-10-08.md), [§10](ARCHITECTURE_REVIEW_2026-10-08.md).

- Control plane runs as user `caoscare-1`; the widget runs in Michael's
  session (`michaelos`); they meet only over local HTTP with a token.
- The widget never does work; it issues control-plane commands and renders
  state. Workers are disposable, one task each; the control plane is the only
  holder of provider and GitHub credentials.
- Project truth stays in each managed project's repo; this repo points at it
  (see §12). Desktop Aria is a different identity from CAOSCare's Aria.

| Component | Label | Citation |
|---|---|---|
| Control plane / widget / worker user split over local HTTP | VERIFIED-IMPLEMENTED | PROJECT_STATE "Stage 1 control plane built"; [`tests/test_widget.py`](../tests/test_widget.py) `test_http_client_against_live_api` |
| Desktop control ("second set of hands") | PROPOSED | [`START_HERE.md`](../START_HERE.md) intro; review [§11](ARCHITECTURE_REVIEW_2026-10-08.md) |

## 2. Control plane core — `desktop_agent/control/`

Entry points: [`__main__.py`](../desktop_agent/__main__.py) (service and
`--check-providers`), [`service.py`](../desktop_agent/control/service.py)
(wires everything; the one object the API and scheduler call).

| Component | Files | Label | Citation |
|---|---|---|---|
| Events, event types, stage derivation (shared contract) | [`events.py`](../desktop_agent/control/events.py) | VERIFIED-IMPLEMENTED | [`tests/test_core.py`](../tests/test_core.py) `test_stage_derivation_sequence`, `test_stage_blocked_and_waiting_owner` |
| Store: SQLite tables + JSONL event log | [`store.py`](../desktop_agent/control/store.py) | VERIFIED-IMPLEMENTED | `tests/test_core.py` `test_store_events_roundtrip_and_jsonl`, `test_tasks_decisions_costs` |
| Receipts (law enforced in code; shared contract) | [`receipts.py`](../desktop_agent/control/receipts.py) | VERIFIED-IMPLEMENTED | `tests/test_core.py` `test_receipt_law` |
| Task contracts (shared contract) | [`contracts.py`](../desktop_agent/control/contracts.py) | VERIFIED-IMPLEMENTED | `tests/test_core.py` `test_project_loader_and_contract` |
| Project loader (descriptor → package) | [`project.py`](../desktop_agent/control/project.py) | VERIFIED-IMPLEMENTED | `tests/test_core.py` `test_project_loader_and_contract`, `test_project_loader_missing_field` |
| Runtime config (scheduler policy, scope limits, providers) | [`config.py`](../desktop_agent/control/config.py) | VERIFIED-IMPLEMENTED | [`tests/test_class_labels.py`](../tests/test_class_labels.py); [`tests/test_providers.py`](../tests/test_providers.py) `test_key_from_file_and_status` |
| Governed MCP tool server for workers (`tools/mcp_server.py`) | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §6](IMPLEMENTATION_PLAN.md); PROJECT_STATE "Stage 1 control plane built (steps 1–6, 8)" records it as deferred |

## 3. Scheduler, admission and feedback

Rule and formula: [`IMPLEMENTATION_PLAN.md` §4](IMPLEMENTATION_PLAN.md).
Concurrency is derived, not a constant.

| Component | Files | Label | Citation |
|---|---|---|---|
| Admission (cpu / memory / budget / ceiling) and owned-area overlap | [`scheduler.py`](../desktop_agent/control/scheduler.py) | VERIFIED-IMPLEMENTED | [`tests/test_adapter_and_sandbox.py`](../tests/test_adapter_and_sandbox.py) `test_admission_and_overlap`; PROJECT_STATE "first planned goal delivered end to end; two workers concurrent" |
| Task lifecycle, retry → escalate → BLOCKED, block cascade | `scheduler.py` | VERIFIED-IMPLEMENTED | [`tests/test_pipeline.py`](../tests/test_pipeline.py) `test_failing_tests_retry_then_block`, `test_max_attempts_one`, `test_block_cascades_to_dependents` |
| Stall detection and kill (`WORKER_STALLED`) | `scheduler.py`, [`launcher.py`](../desktop_agent/control/launcher.py) | HOST-UNVERIFIED | code at `Scheduler` stall check; PROJECT_STATE records no live stall; no test in `tests/` exercises it |
| Provider-limit self-pause and requeue | `scheduler.py` | VERIFIED-IMPLEMENTED | `tests/test_pipeline.py` `test_provider_limit_pauses_and_requeues`; PROJECT_STATE "Run 5 hit the Claude subscription session limit" |
| Feedback metrics, conflict-driven ceiling reduction | [`metrics.py`](../desktop_agent/control/metrics.py) | VERIFIED-IMPLEMENTED | [`tests/test_metrics.py`](../tests/test_metrics.py) `test_outcomes_feedback_and_choice`; PROJECT_STATE "first planned goal delivered end to end" |
| Duplicate-work detection at intake (`duplicate_of`) | [`service.py`](../desktop_agent/control/service.py) | VERIFIED-IMPLEMENTED | [`tests/test_planner.py`](../tests/test_planner.py) `test_duplicate_open_task_is_not_created_twice` |
| Queue derivation when READY is empty (failing runs, defects, acceptance gaps) | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §4](IMPLEMENTATION_PLAN.md) "Queue derivation" |

## 4. Planner and model routing

Class names are `cloud_cheap`, `cloud_strong`, `cloud_max`; the display
labels Luna-class / Sol-class / Astra-class are config
(`DEFAULT_CLASS_LABELS` in [`config.py`](../desktop_agent/control/config.py)).
Local-model strategy: review [§6](ARCHITECTURE_REVIEW_2026-10-08.md).

| Component | Files | Label | Citation |
|---|---|---|---|
| Planner: request → project + bounded contracts, owned-area validation against tracked files | [`planner.py`](../desktop_agent/control/planner.py) | VERIFIED-IMPLEMENTED | `tests/test_planner.py` `test_plan_picks_project_and_validates`, `test_owned_area_validation_against_tracked_files`, `test_unknown_project_becomes_question`; PROJECT_STATE "planner landed; first planned run taught it to see the repo" |
| Router: task type → class, class → model per adapter, escalation ladder | [`router.py`](../desktop_agent/control/router.py) | VERIFIED-IMPLEMENTED | `tests/test_adapter_and_sandbox.py` `test_router` |
| Per-project class floor (`min_model_class`) | `router.py` (`at_least`), `service.py`, [`project.py`](../desktop_agent/control/project.py) | VERIFIED-IMPLEMENTED | [`tests/test_launcher_support.py`](../tests/test_launcher_support.py) `test_model_floor` |
| Class labels in state, panel and widget | `config.py` | VERIFIED-IMPLEMENTED | [`tests/test_class_labels.py`](../tests/test_class_labels.py); [`tests/test_widget.py`](../tests/test_widget.py) `test_class_label_known_unknown_and_missing` |
| Outcome-based class stepping (`choose_class`) | `metrics.py`, `service.py` (called at plan time) | VERIFIED-IMPLEMENTED | `tests/test_metrics.py` `test_outcomes_feedback_and_choice` (the choice function; the service call site is not separately tested); PROJECT_STATE "first planned goal delivered end to end" |
| Structured LLM calls for planner and Aria | [`llm.py`](../desktop_agent/control/llm.py) | VERIFIED-IMPLEMENTED | PROJECT_STATE "planner landed" (real `claude -p` calls, cost recorded); backends in §9 |
| `local_small` models for labelling and routing | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §5](IMPLEMENTATION_PLAN.md) Stage 3 |

## 5. Executor adapters and sandbox

Sandbox design: review [§7](ARCHITECTURE_REVIEW_2026-10-08.md). Adapter
contract: [`adapters/base.py`](../desktop_agent/control/adapters/base.py).

| Component | Files | Label | Citation |
|---|---|---|---|
| `claude_headless` adapter (stream-json → events, claim parsing) | [`adapters/claude_headless.py`](../desktop_agent/control/adapters/claude_headless.py) | VERIFIED-IMPLEMENTED | [`tests/test_adapter_and_sandbox.py`](../tests/test_adapter_and_sandbox.py) `test_adapter_parses_stream`, `test_adapter_denied_and_failed_tests`, `test_adapter_launch_argv`; PROJECT_STATE "Stage 1 acceptance: first real CAOSCare task end to end" |
| `codex_exec` adapter: stream parsing, argv, model map, commit-on-behalf, extra HOME files | [`adapters/codex_exec.py`](../desktop_agent/control/adapters/codex_exec.py) | VERIFIED-IMPLEMENTED | [`tests/test_codex_adapter.py`](../tests/test_codex_adapter.py) (all five tests) |
| Live Codex runs | `codex_exec.py` | HOST-UNVERIFIED | one real run recorded (PROJECT_STATE "Codex executor validated end to end"), but Codex has since been shut down and no live run is repeatable here; the first attempts were BLOCKED by Codex's inner sandbox |
| Per-task clone workspace (local clone + remote fetch; integration checkout never touched) | [`workspace.py`](../desktop_agent/control/workspace.py) | VERIFIED-IMPLEMENTED | [`tests/test_pipeline.py`](../tests/test_pipeline.py) `test_good_worker_end_to_end` (local bare remote) |
| systemd scope + bubblewrap wrapping, per-worker HOME with only credentials, scrubbed worker env | [`sandbox.py`](../desktop_agent/control/sandbox.py) | VERIFIED-IMPLEMENTED | `tests/test_adapter_and_sandbox.py` `test_sandbox_wrap_shapes`, `test_sandbox_wrap_executes`, `test_prepare_worker_home_copies_only_credentials` |
| Launcher: spawn, event stream, kill, per-task test script and prompt | [`launcher.py`](../desktop_agent/control/launcher.py), [`config/worker_prompt.md`](../config/worker_prompt.md) | VERIFIED-IMPLEMENTED | [`tests/test_launcher_support.py`](../tests/test_launcher_support.py) `test_test_script_sets_env_and_runs`, `test_prompt_mentions_script_and_tail_only` |
| Egress allowlist | none | PROPOSED | review [§7](ARCHITECTURE_REVIEW_2026-10-08.md); PROJECT_STATE "egress allowlist deferred with reason" |
| API-key executor (separates worker spend from the interactive limit) | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §7](IMPLEMENTATION_PLAN.md) row 3 |

## 6. Verifier and integrator

| Component | Files | Label | Citation |
|---|---|---|---|
| Verifier: clean checkout, owned-area and shared-contract check, acceptance command | [`verifier.py`](../desktop_agent/control/verifier.py) | VERIFIED-IMPLEMENTED | `tests/test_pipeline.py` `test_good_worker_end_to_end`, `test_failing_tests_retry_then_block`, `test_shared_contract_violation_blocks_without_retry` |
| Integrator: push, `ls-remote` SHA check, draft PR (no merge) | [`integrator.py`](../desktop_agent/control/integrator.py) | VERIFIED-IMPLEMENTED | `tests/test_pipeline.py` `test_good_worker_end_to_end` (push and remote SHA against a local bare remote); PROJECT_STATE "Stage 1 acceptance: first real CAOSCare task" (PR #105 on GitHub) |
| Control-plane commit on a worker's behalf (executor cannot write `.git`) | [`launcher.py`](../desktop_agent/control/launcher.py) (`commit_on_behalf`) | VERIFIED-IMPLEMENTED | [`tests/test_codex_adapter.py`](../tests/test_codex_adapter.py) `test_commit_on_behalf` |
| Control-plane state-file entry after verification (`record_state_entry`) | `integrator.py` | VERIFIED-IMPLEMENTED | [`tests/test_launcher_support.py`](../tests/test_launcher_support.py) `test_control_plane_state_entry_commit` |
| Integration authority beyond draft PRs | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §7](IMPLEMENTATION_PLAN.md) row 4 (open with Michael) |

## 7. Receipts and provenance law

Law: [`AGENTS.md`](../AGENTS.md) "Receipt and provenance law"; model:
review [§8](ARCHITECTURE_REVIEW_2026-10-08.md). Result labels are
`verified`, `unverified`, `simulated`, `failed`.

| Component | Files | Label | Citation |
|---|---|---|---|
| Receipt creation refuses missing provenance | [`receipts.py`](../desktop_agent/control/receipts.py) | VERIFIED-IMPLEMENTED | `tests/test_core.py` `test_receipt_law` |
| Worker claim → `unverified`, verifier → `verified`/`failed`, integrator → `verified` chain | `receipts.py`, `scheduler.py`, `verifier.py`, `integrator.py` | VERIFIED-IMPLEMENTED | `tests/test_pipeline.py` `test_good_worker_end_to_end`, `test_failing_tests_retry_then_block`; PROJECT_STATE "Stage 1 acceptance sequence complete on the control-plane side" |
| Receipt query surface | [`api.py`](../desktop_agent/control/api.py) `GET /v0/receipts` | VERIFIED-IMPLEMENTED | `tests/test_pipeline.py` `test_api_state_goals_events` |

## 8. API v0, panel, widget, server-side Aria

Route table (design): [`IMPLEMENTATION_PLAN.md` §3](IMPLEMENTATION_PLAN.md).
The implemented routes are in [`api.py`](../desktop_agent/control/api.py)
(`/v0/health, projects, state, events, goals, tasks/{id}, intake*, directions,
aria/chat, decisions*, control, receipts`); the panel and widget use the same
API.

| Component | Files | Label | Citation |
|---|---|---|---|
| API v0 with SSE and bearer token | `api.py` | VERIFIED-IMPLEMENTED | `tests/test_pipeline.py` `test_api_state_goals_events`; PROJECT_STATE "Stage 1 control plane built" |
| Instrument panel (single page, same API) | [`panel/index.html`](../panel/index.html) | VERIFIED-IMPLEMENTED | PROJECT_STATE "task `edit-panel-index-html-so-the-cos-457d3e` verified" and "Stage 2 core in place"; no automated UI test, so visual rendering is not test-covered |
| Widget client, direct mode, tool dispatch, SSE refresh coalescer | [`widget/client.py`](../widget/client.py), [`widget/aria.py`](../widget/aria.py) | VERIFIED-IMPLEMENTED | [`tests/test_widget.py`](../tests/test_widget.py) `test_http_client_against_live_api`, `test_direct_mode_confirms_then_submits`, `test_tool_dispatch`, `test_refresh_coalescer_keeps_one_trailing_refresh_per_burst` |
| Widget GTK window displayed in Michael's session | [`widget/app.py`](../widget/app.py) | HOST-UNVERIFIED | PROJECT_STATE "Stage 1 acceptance sequence complete": display check needs Michael ([`WIDGET_SETUP.md`](WIDGET_SETUP.md)) |
| Push-to-talk voice | [`widget/voice.py`](../widget/voice.py) | HOST-UNVERIFIED | no test; needs an OpenAI key and `arecord` ([`WIDGET_SETUP.md`](WIDGET_SETUP.md)) |
| Server-side Aria (`POST /v0/aria/chat`, acts only through control-plane operations) | [`aria.py`](../desktop_agent/control/aria.py), [`widget/aria_prompt.md`](../widget/aria_prompt.md) | VERIFIED-IMPLEMENTED | [`tests/test_aria_brain.py`](../tests/test_aria_brain.py) (all three tests); PROJECT_STATE "Stage 2 core in place" (live probe) |
| Owner-decision inbox (`POST /v0/decisions`) | `api.py`, `service.py` | VERIFIED-IMPLEMENTED | `tests/test_core.py` `test_tasks_decisions_costs`; `tests/test_planner.py` `test_service_plan_goal_then_decision_replans` |

## 9. Provider layer, metering, budgets, unknown-cost accounting

Configuration lives in `RuntimeConfig` ([`config.py`](../desktop_agent/control/config.py));
backends in [`llm.py`](../desktop_agent/control/llm.py). Keys come from a
0600 key file or env var and never reach workers.

| Component | Files | Label | Citation |
|---|---|---|---|
| Key-file / env discovery and status without exposing the key | `config.py` | VERIFIED-IMPLEMENTED | [`tests/test_providers.py`](../tests/test_providers.py) `test_key_from_file_and_status` |
| OpenAI Chat Completions backend, token pricing, error surfacing | `llm.py` | HOST-UNVERIFIED | `test_openai_backend_parses_and_prices`, `test_openai_http_error_surfaces` use fakes; PROJECT_STATE "OpenAI API setup directive" records that no key is installed and no paid call was made |
| Anthropic API backend | `llm.py` | HOST-UNVERIFIED | no test targets `_anthropic`; no Anthropic key is installed on this host |
| `claude_cli` subscription fallback backend | `llm.py` | VERIFIED-IMPLEMENTED | PROJECT_STATE "planner landed" (real structured calls priced in dollars); "Stage 2 core in place" (Aria) |
| Budgets (hourly / daily / per-task) and unknown subscription usage in state | `config.py`, `store.py`, `service.py` | VERIFIED-IMPLEMENTED | `tests/test_providers.py` `test_budgets_and_unknown_usage_in_state` |
| Unknown cost kept unknown (never estimated); prices only if owner-provided | `llm.py`, `metrics.py` | VERIFIED-IMPLEMENTED | `tests/test_providers.py` `test_openai_backend_parses_and_prices`; `tests/test_metrics.py` |
| Per-task budget proxy | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §5](IMPLEMENTATION_PLAN.md) Stage 2 list; PROJECT_STATE "Stage 2 core in place" (needs the API-key decision) |

## 10. Owner-instruction intake and Shared Inbox

Direction: [`OWNER_DIRECTIVES.md`](OWNER_DIRECTIVES.md) (not restated).
Code: [`intake.py`](../desktop_agent/control/intake.py) (poll, ingest,
status, actions), [`intake_delivery.py`](../desktop_agent/control/intake_delivery.py)
(delivery to the project's coordinator). Items persist in the store, so
restarts do not duplicate.

| Component | Label | Citation |
|---|---|---|
| GitHub poll → idempotent items with receipts; replay after restart | VERIFIED-IMPLEMENTED | [`tests/test_intake.py`](../tests/test_intake.py) `test_ingest_deliver_ack_and_replay`; PROJECT_STATE "Communication loop verified end to end on CAOSCARE.COM #117" |
| Delivery: `control_plane` self-ACK; `claude_peer` relay to a live session | VERIFIED-IMPLEMENTED | `tests/test_intake.py` `test_control_plane_coordinator_acks_itself`; PROJECT_STATE "Communication loop verified…" (live delivery, relay cost recorded) |
| ACK / WORKING / BLOCKED / DONE read-back, coordinator activity time | VERIFIED-IMPLEMENTED | `tests/test_intake.py` `test_ingest_deliver_ack_and_replay`; PROJECT_STATE "Communication loop verified…" |
| Backlog batching; delivery failure and unacknowledged flag | VERIFIED-IMPLEMENTED | `tests/test_intake.py` `test_backlog_is_delivered_as_one_batch`, `test_delivery_failure_and_unacked_flag` |
| Shared Inbox directions, item actions, coordinator `ASK-OWNER:` questions | VERIFIED-IMPLEMENTED | `tests/test_intake.py` `test_direction_roundtrip_actions_and_coordinator_question`; PROJECT_STATE "Shared Inbox slice" |
| Live `ASK-OWNER:` question answered from the panel | HOST-UNVERIFIED | PROJECT_STATE "Communication loop verified…": "awaits a real question"; fakes only |
| Live `DONE` link back to test results on the panel | HOST-UNVERIFIED | same entry: supported in code, not yet exercised live |

## 11. Service operations

Run instructions: [`START_HERE.md`](../START_HERE.md) "Running it".

| Component | Files | Label | Citation |
|---|---|---|---|
| User service `desktop-agent.service` on `127.0.0.1:8477` | [`config/desktop-agent.service`](../config/desktop-agent.service) | VERIFIED-IMPLEMENTED | PROJECT_STATE "Stage 1 acceptance: first real CAOSCare task end to end" (the task ran under the installed service) |
| Runtime config (limits, scheduler policy, providers, class labels) | [`config/runtime.yaml`](../config/runtime.yaml) | VERIFIED-IMPLEMENTED | loaded by `RuntimeConfig` at service start (the Stage 1 acceptance entry above); contents are local policy and are not summarised or read for this document |
| Data dir `~/.local/share/desktop-agent`, token file `~/.config/desktop-agent/token`, workspaces `~/Desktop-Agent-work/<task>/` | `config.py` | VERIFIED-IMPLEMENTED | `START_HERE.md` "Running it"; token auth exercised by `tests/test_pipeline.py` `test_api_state_goals_events` |
| Remote access (Tailscale) | none | PROPOSED | [`IMPLEMENTATION_PLAN.md` §5](IMPLEMENTATION_PLAN.md) Stage 2; review [§12](ARCHITECTURE_REVIEW_2026-10-08.md) |

## 12. Project onboarding

A project is one descriptor in `config/projects/`, read by
[`project.py`](../desktop_agent/control/project.py). Fields that matter:
`integration_branch`, `test_command`, owned areas and
`shared_contract_paths`, `min_model_class`, `state_entry_by`
(`worker` or `control`), and an `intake` block naming the owner-dispatch
issue and the coordinator kind.

| Project | Descriptor | Label | Citation |
|---|---|---|---|
| CAOSCare (`state_entry_by: control`, intake #117, `claude_peer` coordinator) | [`config/projects/caoscare.yaml`](../config/projects/caoscare.yaml) | VERIFIED-IMPLEMENTED | PROJECT_STATE "Stage 1 acceptance sequence complete…" (PRs #105, #110) and "Communication loop verified…" |
| Desktop-Agent itself (`state_entry_by: control`, intake #3, control plane as coordinator) | [`config/projects/desktop_agent.yaml`](../config/projects/desktop_agent.yaml) | VERIFIED-IMPLEMENTED | PROJECT_STATE "Stage 2 core in place"; `tests/test_launcher_support.py` `test_state_entry_policy` |
| Michael Business OS (visibility; planned draft-PR docs tasks) | [`config/projects/michael_business_os.yaml`](../config/projects/michael_business_os.yaml) | HOST-UNVERIFIED | PROJECT_STATE "Michael Business OS onboarded for visibility": needs Python ≥ 3.12 and a PostgreSQL-backed suite not present for the control-plane account, no test command, no coordinator integration |

## 13. Staged acceptance

Criteria: [`IMPLEMENTATION_PLAN.md` §5](IMPLEMENTATION_PLAN.md). Dated
evidence: [`PROJECT_STATE.md`](PROJECT_STATE.md). This section summarises
status only; it does not change a criterion.

| Stage | Label | Citation |
|---|---|---|
| Stage 1, control-plane side (three consecutive real CAOSCare runs: DONE, failed-and-shown-failed, DONE; stages from events only) | VERIFIED-IMPLEMENTED | PROJECT_STATE "Stage 1 acceptance sequence complete on the control-plane side" |
| Stage 1, widget criterion (Michael instructs Aria in his own session, opens no terminal) | HOST-UNVERIFIED | same entry: "Still open in the criterion"; [`WIDGET_SETUP.md`](WIDGET_SETUP.md) |
| Stage 2 delivered items: planner, dynamic admission and feedback, stall/retry/escalation, codex adapter, owner inbox, one-writer state entries, duplicate detection, intake | VERIFIED-IMPLEMENTED | PROJECT_STATE "Stage 2 core in place" and the entries cited in §3–§5, §10 |
| Stage 2 acceptance observation (a full real-queue day with Michael touching only the inbox) | HOST-UNVERIFIED | PROJECT_STATE "Stage 2 core in place": "Not done… needs Michael to feed the queue and to run the widget" |
| Stage 2 open items: per-task budget proxy, egress allowlist, Tailscale | PROPOSED | [`IMPLEMENTATION_PLAN.md` §5](IMPLEMENTATION_PLAN.md) Stage 2; see §5, §9, §11 |
| Stage 3: local models and hardware | PROPOSED | [`IMPLEMENTATION_PLAN.md` §5](IMPLEMENTATION_PLAN.md) Stage 3; review [§6](ARCHITECTURE_REVIEW_2026-10-08.md). Not started |

## Keeping this file honest

- This file is a map. When a component changes status, change its row and
  cite the PROJECT_STATE entry or test that justifies it; do not add prose.
- A new component without a row here is undocumented; a row whose link no
  longer resolves is a defect.
- Workers do not edit `PROJECT_STATE.md`; the control plane records each
  verified task there ([`AGENTS.md`](../AGENTS.md) "Project state").
