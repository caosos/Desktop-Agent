# Build Platform — Independent Architecture Review

**Date:** 2026-10-08
**Reviewer:** Claude (Fable 5.1), acting as independent architecture reviewer
**Scope:** pressure-test the proposed persistent-control-plane / disposable-worker architecture before any code is written
**Status:** design review, no implementation

Evidence base: the EliteDesk itself (hardware, installed tools, running
services), `~/CAOSCARE-AGENT-CONTROL/docs/CAOSCARE_AGENT_CONTROL_PLANE.md`,
PR #67 (`Agent Control Plane + persistent Agent 1 foreman loop`), the
bounded-worker `.prompt`/`.out` pairs in `~/CAOSCARE-BOUNDED/`,
`docs/PILOT1_READY_QUEUE.md`, `docs/PILOT1_ACTIVE_WORK.md` (Rules),
`.emergent/summary.txt`, `memory/PRD.md`,
`docs/reports/2026-09-23-milestones-and-decision-history.md`, and current
public documentation for OpenHands, goose, MCP, ACP, Ollama, llama.cpp,
Claude Code, Codex CLI (sources listed at the end).

---

## 0. Ground truth that shapes every recommendation

| Fact | Evidence | Consequence |
|---|---|---|
| EliteDesk = AMD Ryzen 5 PRO 2400GE (4 cores / 8 threads, Zen 1), 14 GB RAM, no discrete GPU, 2 GB swap already 85% used, 56 GB free disk | `nproc`, `free -h`, `lscpu`, `df` | Cannot run coding-grade local models. Can run ≤4B models slowly. Memory is already contested. |
| The same EliteDesk is the **live Pilot 1 room node** (`:8092` backend, `:3000` frontend, Mongo, Home Assistant VM, wake listener). Pilot target is 2026-10-10. | memory + `PILOT1_ACTIVE_WORK.md` | Build workers on this host must be resource-capped and must never touch the room-node ports/DB. Long term the control plane should not share the room node. |
| Installed: `claude` 2.1.293, `codex` CLI, node 24, python 3.10, git 2.34, `bwrap`, `systemd-run`. **Not** installed: tmux, Docker, Podman, Ollama, llama.cpp. | `which` | Two headless coding executors exist today. Sandboxing primitives exist today. No container runtime. |
| Workers are already launched as `claude -p` with a prompt file into a worktree under `~/CAOSCARE-BOUNDED/<task>/`, output captured to a `.out` file. The prompt already contains: worktree, base SHA, task, forbidden actions, gate command, required final output format. | `rq-018-escalation.prompt` / `.out` | **The bounded task contract already exists informally.** The slice is formalising it, not inventing it. |
| 40+ `~/CAOSCARE-*` worktrees, 8 interactive Claude sessions discovered on 2026-10-06, two of them resuming the same session id | control-plane doc §16 | This is the babysitting cost, measured. Fresh workers + control-plane-owned worktrees remove it. |
| CAOSCare already has durable project truth: `CAOSCARE_START_HERE.md`, `AGENTS.md`, `PILOT1_READY_QUEUE.md`, `ENGINEERING_CONTRACT.md`, append-only `PROJECT_STATE.md`, a receipt service with `verified / unverified / simulated / failed` labels and `parent_receipt_id` / `correlation_id` chains. | repo | Do **not** create a second project-truth system. The platform's `project.yaml` points at these files. Reuse the receipt vocabulary. |
| The existing control-plane design (branch `pilot/agent-control-plane`) is built **inside** the CAOSCare FastAPI/Mongo/React admin. PR #67 extends it with a persistent six-agent tmux team and a "foreman" loop. The 2026-10-07 reconciliation says named long-lived agents are history and workers are fresh, bounded, max two. | control-plane doc, PR #67, `PILOT1_READY_QUEUE.md` RECONCILED STATE | Two contradictory runtime models are on the table. This review recommends the fresh-worker model and says what to salvage from #67. |

---

## 1. Challenges to the proposed architecture

The brief's shape (persistent control plane, disposable workers, receipts
with provenance, truthful stages) is right. These are the places where it
should change before building.

1. **The worker should not be the one that pushes, opens PRs, or writes the receipt of record.**
   Brief: worker `COMMITS → WRITES RECEIPT → EXITS`. Keep the local commit.
   Move push, PR creation and the *verified* receipt to the control plane.
   Reason: provenance. A worker's receipt is a self-report, which the
   project's own rule says is never proof. The control plane observed the
   exit code, re-ran the tests, and holds the GitHub credential. The worker
   writes a **claim** (`unverified`); the control plane writes the
   **verification** (`verified` / `failed`). This also keeps GitHub tokens
   out of worker sandboxes.

2. **"Cheapest capable model" is not predictable for coding; use a ladder, not a guess.**
   For coding tasks, default to the strong class and let the cheap class
   handle planning, triage, classification, summaries and routing. A failed
   cheap coding attempt costs tokens *and* a worker slot *and* a test run,
   which on this budget is worse than paying for the strong model first.
   Allow cheap-first only for task types tagged `low_risk` in the model
   policy (docs, config, tests-only), with escalation on failure.

3. **"Continues automatically" needs explicit stop conditions.**
   Add to the control plane policy: queue empty → idle; daily budget cap
   reached → idle; two consecutive BLOCKED on the same task → owner
   decision; any integration failure → stop the train (Michael's existing
   merge-train rule). Michael's rule "idle is better than destructive
   parallelism" should be a hard invariant, not a guideline.

4. **The existing control-plane design put the control plane in the wrong place.**
   `pilot/agent-control-plane` builds it as a CAOSCare admin tab with Mongo
   collections and a relay that delivers commands by having one Claude
   session `SendMessage` another. That tangles product and build tooling,
   makes the care app's database the build system's store, and puts an LLM
   in the delivery path. The build platform is its own program with its own
   store; CAOSCare is project #1 inside it. (Michael's question "do I need a
   new GitHub project" → **yes**, see §12.)

5. **The primitive is "task + worker instance", not "named agent".**
   The registry of `claude-1-coordinator … claude-6-security` is the thing
   that produced the babysitting. PR #67's `caos-agent-01..06` tmux sessions
   with a foreman that "takes another compatible READY task when blocked"
   recreates long-lived sessions with drifting context. Recommend:
   **decline the runtime half of #67** (tmux team, foreman loop, systemd
   recovery unit); **salvage** its receipt-chain code, replay guard,
   instruction validation, and the queue classification work.

6. **`PROJECT_STATE.md` as a worker-appended log will not survive two workers.**
   The control-plane doc already notes "only append-only log files can
   conflict". Once the control plane records every task as receipts, workers
   should stop appending to `PROJECT_STATE.md`; the control plane commits a
   generated entry at integration time, one writer. This is a CAOSCare
   process change and is listed for Michael in §12.

7. **Stage list needs two non-progress states.** `READING, PLANNING,
   BUILDING, TESTING, REVIEWING, INTEGRATING, DONE` plus `BLOCKED` and
   `WAITING_OWNER`. Stage is **derived from events** (table in §8), never
   set by a worker and never a percentage.

8. **The task contract is good; add four fields.** `attempt` (int),
   `supersedes` (task_id or null, for change-direction), `worker_adapter`
   (which executor ran it), `result` (filled by the control plane only).

9. **Desktop control is a different product surface.** It shares the
   control plane, event log and receipts, but its tools must be a separate
   tool class that coding workers can never be granted. Keep it out of the
   first three phases.

---

## 2. Unnecessarily custom (in the brief or in the existing branches)

| Custom thing | Replace with |
|---|---|
| A worker-driver protocol (how the control plane talks to a running worker: prompt in, tool calls / diffs / permission requests out) | **ACP (Agent Client Protocol, Zed)** — JSON-RPC over stdio, already spoken by Claude Code (adapter), Codex (adapter), Gemini CLI, goose, OpenCode and dozens more |
| A tool-governance protocol (how workers get `read project truth`, `run tests`, `write claim receipt`) | **MCP** — one governed MCP server owned by the control plane; every executor above already consumes MCP |
| An agent loop for "non-Claude-Code" providers (Anthropic API, OpenAI API, local models) | **OpenHands Software Agent SDK** (Python, event-sourced, LiteLLM providers incl. OpenAI-compatible local servers) behind the same adapter interface |
| Command relay through a live Claude session (`peer_relay`, control-plane doc §17) | Fresh workers launched by the control plane; nothing to relay |
| Six-agent registry + bindings to live PIDs | Task table + worker-instance table |
| tmux as the process substrate | `systemd-run --user --scope` (resource limits, kill, timeout) + `bwrap` (filesystem); the control plane reads structured stdout, not a terminal |
| Mongo collections in the care app | SQLite + per-task JSONL event files in the platform's own data dir |
| A "foreman" LLM that reconciles the queue continuously | A scheduler that is plain code; an LLM is called only to compile a goal into task contracts |
| A chat interface to the control plane | An instrument panel with one text box for goals and one inbox for owner decisions |

---

## 3. Mature open-source components to reuse (evaluated)

| # | Component | What it is now | Fit | Verdict |
|---|---|---|---|---|
| 1 | **OpenHands Software Agent SDK (V1)** | Python SDK split into SDK / Tools / Workspace / Server; event-sourced conversation state; local or Docker sandbox; providers via LiteLLM (Anthropic, OpenAI, Ollama/llama-server). V0 deprecated April 2026; interfaces still moving. Docker sandbox is "recommended", local process mode is marked unsafe (but that is what `bwrap` is for). | Strong for the **provider-neutral worker loop**. Its event stream maps cleanly onto our event types. | **Reuse as executor adapter #3**, not as the control plane. Do not adopt its server/UI. Pin a version. |
| 2 | **goose (Block / Agentic AI Foundation)** | Rust desktop+CLI agent, MCP-native, ACP server, 15+ providers incl. Ollama, "recipes" for headless runs. | Good proof that MCP + ACP is the standard shape. Interactive-first. | **Optional ACP adapter in Phase 2.** Not the foundation. |
| 3 | **MCP** (spec 2026-07-28: stateless, tasks extension, multi-round-trip requests) | The tool protocol every candidate executor speaks. | Exactly the governed-tools boundary the brief asks for. | **Adopt.** One control-plane MCP server; tool policy = which MCP tools a task may see. |
| 4 | **ACP — Agent Client Protocol (Zed)** (not IBM's Agent *Communication* Protocol) | Client launches agent as subprocess; `session/new`, `session/prompt`, streamed `session/update` (tool calls, diffs, plan), `request_permission`. Zed 1.0 shipped with Claude Code, Codex, Gemini CLI, OpenCode. SDKs in Rust/TypeScript/Python (verify Python kit maturity). | This *is* the worker control protocol. Permission requests map 1:1 to the tool policy. Sessions are interactive-oriented, but "one session, one prompt, wait for end of turn, kill" is a bounded worker. | **Adopt as the generic worker interface.** First adapter can still use native headless JSON streams (`claude -p --output-format stream-json`, `codex exec --json`) because they are installed and proven; ACP generalises them in Phase 2. |
| 5 | **Ollama** | Daemon + model manager, OpenAI-compatible API, tool calls, schema-constrained output. | Convenient; but a daemon that pulls multi-GB models onto a 14 GB production room node is the wrong default. | Dev-box convenience only. |
| 6 | **llama.cpp `llama-server`** | Single binary, OpenAI-compatible, explicit thread/memory control, grammar-constrained JSON, 5–20% faster than Ollama on CPU. | Right for a capped, systemd-managed local inference service. | **Adopt for local inference** (`systemd-run`/unit with `MemoryMax`, `CPUQuota`, `Nice`). Both 5 and 6 are "OpenAI-compatible base URL" to the router, so the provider abstraction has two dialects, not N. |
| 7a | **git worktrees** | Already in daily use. | Ownership + isolation + diff = the integration unit. | **Keep.** Control plane creates and deletes them. |
| 7b | **bubblewrap** (installed) | Unprivileged namespaces; read-only root, rw worktree; optional network unshare. Used by Flatpak, by Codex's bwrap sandbox path and by Claude Code's Linux sandbox. | Right size for one developer's workstation. | **Adopt** as the filesystem layer. |
| 7c | **systemd-run --user** (installed) | Transient scope: `MemoryMax`, `CPUQuota`, `RuntimeMaxSec`, `Nice`, one-command kill. | Right size. | **Adopt** as the resource/lifecycle layer. |
| 7d | **Codex CLI's Landlock/seccomp sandbox**, **Claude Code's bwrap sandbox** | Each executor brings its own. | Useful defence in depth, but the control plane must own the outer sandbox so the policy is uniform across adapters. | Enable both, rely on neither alone. |
| 7e | Docker / rootless Podman | Not installed. | Reproducible test environments; CAOSCare's gate already runs on host with throwaway port + DB. | **Later** (Phase 2) for project #2 or reproducibility; not for the slice. |
| 7f | Firecracker / E2B / Daytona / Managed Agents sandboxes | Hosted or VM-grade isolation. | Budget and host do not justify it. Anthropic **Managed Agents** (hosted, April 2026) is the one to keep in view for burst capacity. | Not now. |
| 7g | Temporal / Prefect / Celery / Kubernetes, LangGraph / CrewAI / AutoGen | Workflow engines and agent-graph frameworks. | Over the problem. Two workers and a SQLite state machine do not need a workflow engine; agent-graph frameworks are not control planes with receipts. | **Do not adopt.** |
| 8 | Provider APIs | Anthropic Messages API (tool use, prompt caching, batch), OpenAI Responses/Chat API, OpenAI-compatible local servers. Claude Agent SDK runs the Claude Code loop in-process (Python/TS). | Direct APIs are the planner/router path. Claude Agent SDK is Claude Code as a library and therefore only ever an **adapter**, consistent with the brief's law. | Router targets: `anthropic`, `openai`, `openai_compatible` (local). |
| 9 | Structured event architecture | ACP `session/update` + MCP tool results + executor-native JSONL. | Everything emits structured events already; the control plane normalises them into one log. | Normaliser per adapter (small). |
| 10 | Safe Linux desktop control | `at-spi2` accessibility tree (semantic, cheaper and safer than screenshots), `ydotool` (uinput, works on Wayland), screenshots via portal/`grim`/`gnome-screenshot`, Playwright for browsers. | Phase 3+. Must be a separate MCP tool server with its own policy, a panic hotkey that kills the scope, and session recording as evidence. | Design only, later. |

---

## 4. Security and autonomy failure modes

| # | Failure | Mitigation (where it lives) |
|---|---|---|
| 1 | **Prompt injection** via repo text, tool output, or web content makes a worker exfiltrate secrets or run destructive commands | Workers hold **no provider or GitHub secrets**. Model calls go through a control-plane proxy that injects credentials and enforces the task budget. Executors that need their own auth (Claude Code OAuth) get a per-worker `HOME` with only the credential file, read-only. `forbidden_actions` enforced by sandbox, not by prompt. |
| 2 | Worker edits outside `owned_area` | `bwrap`: everything read-only except the worktree and `/tmp`; integration gate rejects a diff touching paths outside `owned_area` or in the project's shared-contract list. |
| 3 | Worker touches the live room node (`:3000`, `:8092`, Mongo `caoscare`) | Per-task ports + throwaway DB in the contract (already practised); Phase 2 adds network namespace + egress allowlist proxy so only provider/GitHub hosts are reachable. |
| 4 | **Self-report as truth** ("tests passed", "pushed") | The TESTING and INTEGRATING stages are control-plane-owned: it re-runs `acceptance_tests` in a clean checkout and confirms the SHA on the remote with `git ls-remote` before any `verified` receipt. |
| 5 | Runaway cost / runaway loop | `budget` in contract enforced by the proxy (tokens and USD); `RuntimeMaxSec`; `max_turns`; retry budget of 2; daily cap for the whole platform. |
| 6 | Host resource exhaustion on a shared production box | `MemoryMax`, `CPUQuota`, `Nice` per worker scope; max two workers; local inference capped separately; swap already near full → set `MemoryMax` conservatively (≤ 3 GB per worker) until the control plane leaves the room node. |
| 7 | Two writers on one branch/session (seen: session `db41a1a3` resumed twice) | Fresh worker per task, control-plane-created worktree, worker cannot `--resume`. |
| 8 | Merging broken work | One branch at a time; control plane's own test run on the merge result; stop at first regression (existing merge-train rule, now enforced by code). |
| 9 | Secrets leaking into events, receipts, PRs | `.env` and credential paths in the project's `never_read` list → excluded by sandbox mount; event payloads store paths, hashes and diffs, never environment dumps; redaction pass before any receipt is committed to the repo. |
| 10 | Fake progress | Stage is a pure function of the event log; the UI has no other input. |
| 11 | Owner-decision fatigue → Michael stops reading → silent drift | Owner decisions have a taxonomy (§12 item 5). Anything not in it proceeds under policy. The inbox shows the one next decision (Michael's "execute one thing" rule). |
| 12 | Desktop tools clicking through dangerous dialogs | Separate tool class, never co-granted with coding tools, panic hotkey, recording. Not before Phase 3. |
| 13 | Executor drift (Claude Code / Codex change their JSON output) | Adapter boundary + a fixture test per adapter; ACP reduces exposure in Phase 2. |

---

## 5. Minimum viable architecture

```
┌──────────────────────────── EliteDesk (later: any Linux box / Pi for control only) ───────────────────────────┐
│                                                                                                               │
│  panel (web, SSE)  ◄── read model ──┐                                                                        │
│                                      │                                                                        │
│  control  (one Python process, systemd --user service)                                                        │
│   ├─ project loader      reads <repo>/.agentproject/project.yaml → START_HERE, AGENTS, ready_queue, policies │
│   ├─ task compiler       goal (text) → 1..n task contracts   [one planner model call, cheap class]           │
│   ├─ scheduler           ready queue, dependencies, max 2 workers, stop conditions                           │
│   ├─ model router        model_class → provider+model; cost ledger; budget enforcement proxy                 │
│   ├─ worker launcher     worktree → systemd-run scope → bwrap → executor adapter                              │
│   │     adapters: claude_headless (now) │ codex_exec (now) │ acp_generic (P2) │ openhands_sdk (P2)            │
│   ├─ governed tools      MCP server: project_truth.read, tests.run, claim_receipt.write, …                   │
│   ├─ event log           append-only JSONL per task + SQLite index  → stage derivation                       │
│   ├─ verifier            re-runs acceptance tests in clean checkout; checks remote SHA; writes receipts      │
│   └─ integrator          push, draft PR or merge-train (policy), one at a time, stop on regression           │
│                                                                                                               │
│  workers (disposable)   worktree_i + scope_i + bwrap_i → executor → exits                                     │
│  local inference        llama-server (≤4B, capped)  — classification/summaries only                         │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
          │ Anthropic API / OpenAI API via proxy                     │ GitHub (control plane credential only)
```

Boundaries:

- **control** owns: task contracts, worktrees, launching, budgets, events,
  receipts, verification, integration. It never edits code.
- **worker** owns: reading, building, local commits, a claim receipt. It
  never pushes, never merges, never sees a secret, never outlives its task.
- **project truth** lives in the project repo. The platform stores only
  runtime state (tasks, events, receipts, costs) in its own data directory.
- **panel** is a read model of the event log plus four commands
  (goal, stop, pause, decide).

Language: Python. Reasons: CAOSCare backend is Python, OpenHands SDK is
Python, MCP and ACP have Python kits, and one language keeps the thing
auditable by the same people.

---

## 6. Local-model strategy

Honest capacity of the current hardware (estimates; measure with
`llama-bench` before deciding):

| Host | Model size (Q4) | Expected decode speed | Memory | Use |
|---|---|---|---|---|
| EliteDesk (2400GE, 14 GB, shared with room node) | 1.5–2B | ~12–20 tok/s | ~1.5 GB | routing, labelling |
| EliteDesk | 3–4B | ~6–10 tok/s | ~3 GB | classification, summaries, receipt text, blocked-vs-owner triage |
| EliteDesk | 7–8B | ~3–5 tok/s | ~6 GB | **does not fit safely** next to the room node + HA VM today |
| Raspberry Pi 5 (16 GB) | 3B | ~4–7 tok/s | ~3 GB | same roles as above, on a box that is not the room node |
| Raspberry Pi 5 + AI HAT+ 2 | ≤1.5B supported | 20–50 tok/s | — | small-model only; no Qwen3 4B/8B support listed |
| Any of the above | coding models (30B-A3B MoE needs ~18 GB; dense 14B+) | — | — | **not viable** |

Strategy:

1. **Three model classes in `model_policy.yaml`:** `local_small`,
   `cloud_cheap`, `cloud_strong`. Map task types to classes; coding →
   `cloud_strong` by default; planning/triage/classification →
   `cloud_cheap`; event labelling, summaries, routing decisions →
   `local_small` when available, `cloud_cheap` fallback.
2. **Local models do background work that is high-volume and low-stakes:**
   classify each event (is this a blocker? owner decision? external?),
   summarise worker output into the panel's "last activity" line, draft
   receipt text, embed project docs for `read_list` selection. Every local
   output is labelled `inferred` in provenance.
3. **Serve with `llama-server`** under a user systemd unit with
   `MemoryMax`, `CPUQuota=200%`, `Nice=10`, OpenAI-compatible endpoint. The
   router treats it as `openai_compatible`. Ollama remains fine on a
   development laptop.
4. **Raspberry Pi:** useful as the **always-on control-plane host** (the
   control plane is I/O-bound, a Pi 5 runs it easily) plus a small local
   model for routing, which frees the EliteDesk room node from build-platform
   load entirely. Not useful as a coding worker host. Workers then run on
   the EliteDesk (or a future stronger box, or Managed Agents) and the Pi
   talks to them over SSH/ACP-over-HTTP (goose's planned transport) — a
   Phase 2+ topology, not a Phase 0 one.
5. **Escalation ladder** is allowed only for `low_risk` task types; a
   failed `cloud_cheap` coding attempt escalates once to `cloud_strong`,
   then BLOCKED.

---

## 7. Execution and sandbox strategy

Four layers, all already available on the host except the egress proxy:

| Layer | Mechanism | Enforces |
|---|---|---|
| 1 Ownership | `git worktree add` from `base_ref`, one per task, created and removed by control | `owned_area`, `base_ref`, clean diff |
| 2 Lifecycle & resources | `systemd-run --user --scope -p MemoryMax= -p CPUQuota= -p RuntimeMaxSec= -p Nice=` | `budget.wall_clock`, memory, kill/stop/pause (SIGSTOP/SIGCONT the scope) |
| 3 Filesystem & process | `bwrap --ro-bind / / --bind <worktree> <worktree> --tmpfs /tmp --dev /dev --proc /proc --unshare-pid --die-with-parent`, minimal `HOME`, no `.env`, no `~/.ssh`, no other worktrees | `forbidden_actions` (filesystem class), `never_read` list |
| 4 Verification gate | control re-runs `acceptance_tests` on a clean checkout of the worker's commit; checks paths vs `owned_area`; checks remote SHA | truth of "tests passed", "pushed" |

Network: Phase 0 keeps the host network (executors need provider and
package access) but strips credentials. Phase 2 adds `--unshare-net` plus
a local egress proxy that allows provider hosts, GitHub and package
registries only; this is also where the per-task token budget is enforced
for executors that call providers directly.

Executors' own sandboxes (Codex Landlock/seccomp, Claude Code bwrap) stay
on as inner layers. Containers (rootless Podman) come when a project needs
a reproducible environment that the host cannot provide; CAOSCare does not
today.

---

## 8. Durable state, event and receipt model

**Project package** (lives in the project repo, e.g. `.agentproject/`):

```
project.yaml        name, repo, default_branch, integration_branch,
                    start_here: CAOSCARE_START_HERE.md
                    agents_file: AGENTS.md
                    ready_queue: docs/PILOT1_READY_QUEUE.md     ← points, does not copy
                    current_state: docs/PROJECT_STATE.md
                    acceptance: docs/ENGINEERING_CONTRACT.md + per-task
                    shared_contract_paths: [backend/models.py, backend/server.py, …]
                    never_read: [.env, backend/.env, **/credentials*]
                    test_command: backend/scripts/run_backend_tests.sh  (with port/db env)
model_policy.yaml   task_type → model_class; low_risk types; escalation rule; daily cap
tool_policy.yaml    tool class → MCP tools; default allowed_tools per task_type; forbidden_actions
```

Runtime config (ports, data dir, provider endpoints, scope limits) is
control-plane-local, never in the project repo.

**Task contract** (JSON, stored by control, hash recorded in every event):
the brief's fields `task_id, objective, why_now, project, base_ref,
owned_area, read_list, acceptance_tests, allowed_tools, forbidden_actions,
model_class, budget, dependencies, expected_artifacts` plus `attempt,
supersedes, worker_adapter, result` (§1.8).

**Event** (append-only, one JSONL file per task, indexed in SQLite):

```
{ id, ts, task_id, worker_id|null, type, payload,
  provenance: { actor: human|control|worker|provider|inferred,
                source: "claude_headless"|"codex_exec"|"verifier"|"panel"|…,
                contract_hash, model, evidence: [paths, sha, exit_code, hashes] },
  parent_event_id }
```

Types: `GOAL_RECEIVED, TASK_CREATED, TASK_CLAIMED, WORKER_STARTED,
MODEL_SELECTED, FILE_READ, FILE_CHANGED, TOOL_CALLED, TOOL_DENIED,
TEST_STARTED, TEST_PASSED, TEST_FAILED, COMMIT_CREATED, CLAIM_WRITTEN,
BLOCKED, OWNER_DECISION_REQUESTED, OWNER_DECISION_RECORDED, BUDGET_WARNING,
WORKER_FINISHED, WORKER_KILLED, VERIFY_STARTED, VERIFY_PASSED,
VERIFY_FAILED, PUSHED, PR_OPENED, INTEGRATION_STARTED, INTEGRATION_PASSED,
INTEGRATION_FAILED, TASK_DONE`.

**Stage derivation** (pure function of the latest events):

| Latest significant event | Stage |
|---|---|
| `WORKER_STARTED` … first `FILE_CHANGED` | READING |
| `TASK_CREATED` without worker / planner running | PLANNING |
| `FILE_CHANGED` seen, no `TEST_STARTED` yet | BUILDING |
| `TEST_STARTED` (worker) or `VERIFY_STARTED` (control) | TESTING |
| `WORKER_FINISHED` + `VERIFY_PASSED`, diff under review / PR open | REVIEWING |
| `INTEGRATION_STARTED` | INTEGRATING |
| `TASK_DONE` | DONE |
| `BLOCKED` | BLOCKED |
| `OWNER_DECISION_REQUESTED` unanswered | WAITING_OWNER |

**Receipt** (reuses CAOSCare vocabulary):

```
{ receipt_id, subject: {type: task|goal|integration, id}, claim,
  actor, result_label: verified|unverified|simulated|failed,
  evidence: [event_ids, commit_sha, remote_sha_check, test_exit_code, diff_hash],
  before_state, after_state, parent_receipt_id, correlation_id (= goal id), ts }
```

Rules: a worker can only produce `unverified`; `verified` requires the
verifier's own observation; every receipt has `actor` + `evidence`
(no receipt without provenance); every state change has a receipt
(no action without a receipt). At integration the control plane commits a
generated, one-writer summary into the project's `current_state` file.

---

## 9. UI architecture

- **One page, served by control**, data over Server-Sent Events from the
  event log; no polling, no second backend. Small React app (the team
  already works in React) or plain HTML + htmx; either is fine, keep it
  under a few hundred lines to start.
- **Instrument panel layout** (top to bottom): project · current goal ·
  stage strip (the nine stages, current one lit) · workers (≤2 cards:
  task, model, adapter, elapsed, last activity line) · tests (last
  verifier run: pass/fail counts, exit code) · files changed (diff summary,
  click for diff) · receipts (chain for the current task) · blocked items ·
  owner decision inbox (one decision at a time, yes/no/text) · next task ·
  cost/usage (today, this goal, per task) · controls.
- **Controls** map to control-plane commands with receipts:
  `stop` (SIGTERM the scope, `WORKER_KILLED`, task → BLOCKED with reason
  "owner stop"), `pause` (scheduler stops launching; running worker
  finishes), `change direction` (new goal; current task gets `supersedes`
  and is cancelled or allowed to finish per a toggle), `decide`.
- **No percentages, no "thinking…" animations.** Every visible number has
  an event behind it; "last activity" is the newest event's summary and
  its age.
- **Remote:** LAN first; Tailscale when Michael wants it on his phone
  (decision §12.8). The page must already work at phone width.

---

## 10. What should NOT be built

- A custom agent loop before the adapters and OpenHands SDK are exhausted.
- A custom worker-driver protocol (ACP exists) or tool protocol (MCP exists).
- The persistent six-agent tmux team, foreman loop and systemd recovery unit from PR #67.
- Command relay through a live Claude session.
- The control plane inside CAOSCare's backend, Mongo or admin tab.
- Any percentage progress, ETA, or "confidence" display.
- A chat window to the control plane.
- Workflow engines, message queues, Kubernetes, vector databases, multi-tenancy, cloud deployment of the control plane.
- Desktop control in Phases 0–2.
- Local coding models on the EliteDesk or a Pi.
- Docker/Podman before a project needs a reproducible environment.
- A second project-truth system that copies what `START_HERE`, `AGENTS.md`, the ready queue and `ENGINEERING_CONTRACT` already say.
- Model fine-tuning of any kind.

---

## 11. Phased design

### Phase 0 — Prototype: the vertical slice (prove the machine)

Scope exactly as the brief: panel → one project (CAOSCare) → read
bootloader → instruction → task contract → model selected → worktree →
worker executes → events stream → tests run → diff displayed → commit
created → receipt stored → worker exits.

- Executor: `claude_headless` only (`claude -p --output-format stream-json`, `--allowedTools`, `--max-turns`, `--mcp-config` pointing at the governed MCP server). Codex adapter is cheap to add once the adapter interface is proven.
- Task compiler: a template that turns a goal + project package into one contract (the existing `.prompt` format is the template's ancestor). No planner model yet.
- Scheduler: one worker.
- Sandbox: layers 1–3 (worktree, systemd scope, bwrap). Host network.
- Store: SQLite + JSONL. Receipts as in §8.
- Verifier: re-run `test_command` on a clean checkout; `git ls-remote` check after control pushes.
- Panel: project, goal, stage strip, one worker card, events tail, tests, diff, receipts, stop.
- **Exit criterion:** Michael types one real READY-queue task (e.g. a docs or test task) into the panel, watches the stages move on real events, sees the diff and a `verified` receipt, and never opens a terminal. Repeat three times.

### Phase 1 — Useful (replace the babysitting)

- Planner: goal → multiple contracts via a `cloud_cheap` call, reading the ready queue and `AGENTS.md`; dependencies honoured.
- Two workers; `codex_exec` adapter; model router with three classes and the cost ledger; per-task budget via proxy.
- Blocked handling and the owner-decision inbox with the taxonomy.
- Integrator: draft PR by default; merge-train mode behind policy (§12.4).
- One-writer `current_state` entry at integration; workers stop editing `PROJECT_STATE.md`.
- `local_small` for event labelling and activity lines via `llama-server`, if memory allows; otherwise `cloud_cheap`.
- Tailscale for the phone (if decided).
- **Exit criterion:** a full READY-queue day runs with Michael touching only the inbox; every merged change has a `verified` chain; cost per task is visible.

### Phase 2 — Autonomous (continues on its own, safely)

- Automatic continuation with stop conditions and retry budget.
- `acp_generic` adapter (goose, Gemini CLI, any ACP agent) and `openhands_sdk` adapter for direct Anthropic/OpenAI/local providers.
- Network namespace + egress allowlist proxy.
- Rootless Podman test environments for projects that need them; second project onboarded to prove `project.yaml` generality.
- Control plane moves off the room node (Pi or another box) if Phase 1 showed contention.
- Desktop tool server designed (not granted to coding workers); Managed Agents evaluated for burst.
- **Exit criterion:** platform runs a second project end-to-end and the room node shows no build-platform load.

---

## 12. Decisions that genuinely require Michael

1. **New repository for the platform.** Recommended: yes, a separate repo (CAOSCare becomes its first managed project; this document becomes its first `docs/` file). Name is Michael's.
2. **Where the control plane and workers run during Pilot 1.** The EliteDesk is the live room node and Pilot 1 is dated 2026-10-10. Options: (a) run capped workers on the EliteDesk now, (b) hold the slice until after the pilot, (c) another Linux box or a Pi for the control plane. Recommendation: (a) with `MemoryMax` ≤ 3 GB per worker and no local inference until the pilot room is stable.
3. **How model usage is paid.** API keys (metered, per-task budgets enforceable, provider-neutral) versus subscription-backed Claude Code headless (fixed cost, subject to the Agent SDK credit limits). This decides whether `claude_headless` or a direct-API loop is the long-term primary executor. Both can coexist; the default matters for budgeting.
4. **Integration authority.** May the control plane merge into `integration/2026-09-27` on its own when the diff stays inside `owned_area`, touches no `shared_contract_paths`, and the verifier passes? Or always draft PR + Michael merge? (`main` and Linode stay Michael-only regardless.)
5. **Owner-decision taxonomy.** Proposed list to ratify: product/behaviour choices, anything touching real resident data, spend above a per-task or daily cap, shared-contract changes, external accounts/hardware, `main`/production. Everything else proceeds under policy.
6. **PR #67.** Adopt or decline its runtime half (persistent tmux team, foreman). This review recommends decline, salvage the code noted in §1.5. Michael authorised the persistent model on 2026-10-07 and the same day's reconciliation says the opposite; one of them has to be the rule.
7. **`PROJECT_STATE.md` authorship.** Move from worker-appended to control-plane-generated at integration (§1.6).
8. **Remote access.** Tailscale (recommended) versus an outbound relay through Linode. Not needed for Phase 0.

Everything else in this document (ports, file layout, SQLite vs. Postgres,
React vs. htmx, exact scope limits, adapter order) can be changed later
without consulting Michael and is therefore not a question.

---

## Sources consulted (public)

- OpenHands SDK: [MLSys 2026 paper](https://proceedings.mlsys.org/paper_files/paper/2026/hash/8ae9cf363ea625161f885b798c1f1f78-Abstract-Conference.html), [arXiv 2511.03690](https://arxiv.org/html/2511.03690v1), [sandbox overview](https://docs.openhands.dev/openhands/usage/sandboxes/overview.md), [custom sandbox guide](https://docs.openhands.dev/openhands/usage/advanced/custom-sandbox-guide), [V0 deprecation note](https://github.com/NousResearch/hermes-agent/issues/477)
- goose: [AAIF on goose + ACP](https://aaif.io/blog/where-new-mcp-ideas-go-to-become-real-goose-as-a-proving-ground), [goose and ACP](https://themenonlab.blog/blog/goose-acp-agent-client-protocol-interoperability), [review](https://andrew.ooo/posts/goose-review-open-source-ai-agent-block/)
- ACP: [Zed ACP progress report](https://zed.dev/blog/acp-progress-report), [ACP overview](https://www.morphllm.com/agent-client-protocol), [ACP vs MCP](https://mcp.directory/blog/agent-client-protocol-vs-mcp-2026)
- MCP: [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [AAIF summary](https://aaif.io/blog/mcp-2026-changes-the-rules)
- Ollama / llama.cpp: [comparison (April 2026)](https://www.respan.ai/market-map/compare/llama-cpp-vs-ollama), [function calling local LLMs](https://insiderllm.com/guides/function-calling-local-llms/), [Pi 5 Ollama vs llama.cpp](https://medium.com/@omkar121212/ollama-vs-llama-cpp-on-raspberry-pi-5-8e7fbeb310de)
- Local coding models: [Unsloth Qwen3-Coder guide](https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally.md), [Kilo local coding models](https://blog.kilo.ai/p/the-best-local-coding-models-for)
- Raspberry Pi: [Pi 5 LLM benchmarks](https://stratospherelinuxips.readthedocs.io/en/develop/immune/research_rpi_llm_performance.html), [AI HAT+ 2](https://raspberry.tips/en/?p=9674), [agents on Pi 5](https://www.wemustbegeeks.com/how-to-run-ai-agents-on-a-raspberry-pi-5/)
- Claude Code / Agent SDK / Managed Agents: [headless mode](https://docs.claude.com/en/docs/claude-code/headless), [Agent SDK](https://code.claude.com/docs/en/agent-sdk.md), [SDK vs Managed Agents](https://hatchworks.com/blog/claude/claude-agent-sdk-and-managed-agents/)
- Codex CLI: [security / sandboxing](https://developers.openai.com/codex/security), [sandboxing architecture mirror](https://www.mintlify.com/openai/codex/architecture/sandboxing)
- Sandboxing: [bubblewrap](https://github.com/containers/bubblewrap), [nsjail overview](https://www.morphllm.com/nsjail-sandbox), [Stanford jai comparison](https://jai.scs.stanford.edu/comparison.html)
