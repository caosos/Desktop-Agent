# Implementation plan — Desktop-Agent

**Status:** DESIGN. Nothing here is built. Implementation starts only when
Michael says BUILD (`OWNER_DIRECTIVES.md` 2026-10-08, point 12).

This plan turns the architecture review (`ARCHITECTURE_REVIEW_2026-10-08.md`)
and the 2026-10-08 directive into a buildable sequence. Where the two
differ, the directive wins and the difference is named.

---

## 1. System shape

```
 Michael's GNOME session (user michaelos)         caoscare-1 user (repos, claude, codex, worktrees)
 ┌──────────────────────────────┐                 ┌────────────────────────────────────────────────┐
 │  Aria widget (GTK4, Python)  │   HTTP + SSE    │  control plane (systemd --user service)        │
 │  text · push-to-talk · state │ ──────────────► │  api · scheduler · launcher · router · events  │
 │  projects · inbox · open panel│ ◄────────────── │  receipts · verifier · integrator · MCP tools  │
 └──────────────────────────────┘  127.0.0.1:port │        │                                       │
 ┌──────────────────────────────┐  bearer token   │        ▼                                       │
 │  web instrument panel        │ ──────────────► │  workers: worktree + systemd scope + bwrap     │
 │  (inspection, any browser)   │                 │  → claude_headless | codex_exec | … → exit     │
 └──────────────────────────────┘                 └────────────────────────────────────────────────┘
                                                           │ Anthropic / OpenAI APIs via proxy (API keys)
                                                           │ GitHub (control-plane credential only)
```

Facts that fix this shape (checked 2026-10-08 on the EliteDesk):

- The desktop session (`seat0`) belongs to user `michaelos`; the repos,
  `claude`, `codex` and all worktrees belong to `caoscare-1`. The widget and
  the control plane therefore run as different users and talk only over a
  local HTTP socket with a token. Nothing is shared through `HOME`.
- PyGObject with GTK 4.6 is available to Python 3.10. A native widget needs
  no Electron or Tauri.
- `xdotool` is installed, which suggests an X11 session; the session type
  is still to be confirmed when BUILD starts (affects global hotkeys and,
  much later, desktop control).
- Three projects are named in the directive. CAOSCare is at
  `~/CAOSCARE-INTEGRATION`. "Deal Sniffer" has no repository by that name;
  the closest is `caosos/michael-business-os` (opportunity discovery, deal
  scoring). To be confirmed by Michael before onboarding. Desktop Agent is
  this repository.

Invariant: **the widget never does work.** It issues control-plane commands
and renders control-plane state. Every command it issues is a receipt with
actor `widget:aria` and the raw utterance as evidence.

## 2. The Aria desktop widget

**Identity.** Michael calls both the CAOSCare resident assistant and this
desktop assistant "Aria". They are two products. Rule: separate prompt
file (in this repo), separate memory, separate credentials; nothing is
shared or imported from CAOSCare's Aria. The name is the only thing in
common.

**Form.** A small always-on-top window (about 320 × 440 px), docked to a
screen corner, togglable with a GNOME custom shortcut that runs
`aria-widget --toggle`. GNOME has no system tray by default, so the window
itself is the presence. It must work at that size without scrolling.

**Layout, top to bottom.**

| Zone | Content | Source of truth |
|---|---|---|
| Header | `ARIA` · state chip | control-plane state |
| State | one of `Ready · Listening · Thinking · Working (n) · Awaiting owner (n) · Blocked · Offline` | derived from events; `Offline` when the API is unreachable |
| Prompt | "What would you like me to work on?" then the last short reply | conversational layer |
| Input | text box + push-to-talk button | widget |
| Inbox | badge + the single next owner decision, with its yes / no / text answer | `OWNER_DECISION_REQUESTED` events |
| Projects | one row per onboarded project: name · stage chip · last activity line and age | control-plane state |
| Footer | `Open panel` (web instrument panel in the browser) · `Pause` · `Stop` | control-plane commands |

No percentages, no spinners that mean nothing. The state chip and stage
chips are the only animation, and they change only on events.

**Conversational layer ("Aria" proper).** A `cloud_cheap` model called by the
widget with a tool set that is exactly the control-plane API below:
`list_projects, get_state, submit_goal, explain_task, answer_decision,
pause, resume, stop`. It has no file, shell, browser or desktop tools. Its
prompt lives at `widget/aria_prompt.md` (one source). It confirms a goal in
one sentence (project + objective) before submitting, answers status
questions from `get_state`, and refuses anything outside its tools by
saying what the panel can show instead.

**Voice.** Push-to-talk only; no always-on wake word on Michael's desk.
Audio → cloud transcription (OpenAI transcription API, the path the team
already knows) → text path. Spoken reply is optional TTS of the same short
text. Stage 1 ships text first, voice second, in that order.

**Connection.** `127.0.0.1:<port>` (configurable), bearer token generated
by the control plane on first run and written to a file the `michaelos`
user can read (group-readable, mode 0640) or pasted once into the widget's
config. SSE for live state; the widget keeps no durable state and
reconnects on loss, showing `Offline` until it does.

## 3. Control-plane API (v0, the widget's whole world)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v0/projects` | onboarded projects with stage and last activity |
| GET | `/v0/state` | snapshot: workers, tasks, inbox, costs, blockers |
| GET | `/v0/events?since=<cursor>` | SSE stream of normalised events |
| POST | `/v0/goals` | `{project, text}` → goal id, compiled task ids |
| GET | `/v0/tasks/{id}` | contract, stage, events tail, diff summary, receipts |
| POST | `/v0/decisions/{id}` | `{answer}` → `OWNER_DECISION_RECORDED` |
| POST | `/v0/control` | `{action: pause|resume|stop, task_id?}` |
| GET | `/v0/receipts?task_id=` | receipt chain |

Every mutating call carries `Idempotency-Key` (replay guard, as in the
CAOSCare control-plane code worth salvaging) and produces a receipt. The web
panel uses the same API; there is no second one.

## 4. Scheduler: dynamic concurrency (supersedes "max two")

The directive's principle, as a rule the scheduler can execute:
**maximise verified work per dollar and per hour**, not worker count.

**Admission.** Every scheduling tick computes how many workers may run:

```
cpu_slots      = floor((cores − reserved_cores) / cores_per_worker)
mem_slots      = floor((mem_available − host_reserve) / mem_per_worker)
budget_slots   = floor((hourly_cap − spend_last_hour) / expected_cost_per_worker_hour)
provider_slots = per provider: concurrency / rate limit − in-flight
ceiling        = configured hard ceiling (safety, owner-set)
slots          = min(cpu_slots, mem_slots, budget_slots, Σ provider_slots, ceiling)

eligible = READY tasks whose dependencies are DONE
           and whose owned_area does not overlap any running task's owned_area
           and whose shared_contract_paths do not collide

launch = min(slots, |eligible|)   // ordered by priority, then smallest owned_area
```

On the EliteDesk today the memory term alone yields about two slots next to
the live room node; that is a derivation, not a constant, and it changes
when the host or the budget changes.

**Feedback.** Over a rolling window the scheduler tracks
`verified_tasks / dollars`, `verified_tasks / hour`, `conflict_rate`
(integration failures caused by overlapping edits) and `retry_rate`. If
`conflict_rate` or the integration backlog rises, the ceiling drops by one;
after N clean integrations it rises by one, never above the owner-set
ceiling. The panel shows these four numbers; they are the honest measure of
the directive's principle.

**Stall, retry, escalate.** No events from a worker for `stall_after`
(default 10 min) → `WORKER_STALLED`, then kill the scope and record the
attempt as failed. Retry once at the same model class; on a second failure
escalate one class (`cloud_cheap → cloud_strong`) if the policy allows for
that task type; on a third failure → `BLOCKED` with the evidence. Never
retry a task whose failure was a `TOOL_DENIED` on a forbidden action; that
goes straight to `BLOCKED`.

**Queue derivation.** When READY is empty the planner (a `cloud_cheap`
call) proposes tasks from, in order: failing verifier runs, verified
defects recorded in receipts, unmet acceptance criteria in the project's
acceptance file, and approved requirements in the project's queue file.
Proposals enter the queue as `PROPOSED` and become `READY` under policy
(task type allowed to auto-approve) or go to the inbox. Nothing cosmetic.

**Stop conditions** (hard): daily budget cap; `BLOCKED` on every eligible
task; integration failure (train stops); owner `pause` or `stop`.

## 5. Stages and what each proves

### Stage 1 — Desktop widget + cloud AI + one task end to end

Equals the review's Phase 0 slice plus the widget. Build order:

1. `control/store` (SQLite + JSONL events), `control/events` (types, stage derivation), `control/receipts`.
2. `control/project` loader reading CAOSCare's `.agentproject/project.yaml` (added to CAOSCare as a small PR when BUILD starts; it only points at existing files).
3. `control/launcher` with the `claude_headless` adapter, worktree + `systemd-run` scope + `bwrap`.
4. `control/verifier` (clean checkout, test command, `git ls-remote`), `control/integrator` (push + draft PR; no merge).
5. `control/api` (table in §3) with SSE.
6. `panel/` minimal web page on the same API.
7. `widget/` GTK4 app: text first, then push-to-talk; `aria_prompt.md`; conversational layer with the API tool set.
8. Scheduler in its single-slot form (admission formula present, ceiling = 1).

**Acceptance (the directive's "first things first"):** Michael types or
says to the widget one real CAOSCare READY task of docs or test scope.
Aria confirms project and objective in one sentence. The widget shows
`Working (1)` and the project row moves READING → BUILDING → TESTING →
REVIEWING → DONE on real events. The panel shows the diff, the verifier's
test output, a `verified` receipt chain and a draft PR link. Michael
opened no terminal. Three consecutive runs, including one that fails
verification and is shown truthfully as failed.

### Stage 2 — Autonomous coordination

Review Phases 1 and 2 with the §4 scheduler at full strength: planner and
queue derivation, dynamic admission and feedback, stall/retry/escalation,
`codex_exec` adapter, per-task budget proxy, owner inbox with the directive's
taxonomy, integration policy per review §12.4, one-writer project-state
entries, second project onboarded (Deal Sniffer, once its repository is
confirmed), egress allowlist, Tailscale if wanted.

**Acceptance:** a full day on a real queue with Michael touching only the
inbox; every integrated change has a `verified` chain; the panel's four
feedback numbers are populated; no edit conflict reached integration.

### Stage 3 — Local models and hardware

Review §6 unchanged: `local_small` for labelling, summaries and routing on
whatever host the economics justify; coding stays on cloud until hardware
exists that can run coding-grade models. No interface or control-plane
change; providers change in `model_policy.yaml`.

## 6. Repository layout (when BUILD is authorised)

```
desktop_agent/
  control/   api.py  scheduler.py  launcher.py  router.py  store.py
             events.py  receipts.py  verifier.py  integrator.py  project.py
             adapters/  base.py  claude_headless.py  codex_exec.py
  tools/     mcp_server.py            # governed tools for workers
  widget/    app.py  aria.py  client.py  aria_prompt.md
  panel/     index.html               # instrument panel
tests/
config/      runtime.example.yaml  model_policy.example.yaml  tool_policy.example.yaml
docs/
```

Every implementation file stays under ~300 lines (`AGENTS.md`). Project
packages (`.agentproject/`) live in each managed project's repo, never here.

## 7. Decision status after the directive

| Review §12 item | Status |
|---|---|
| 1 Separate repo | **Answered**: `caosos/Desktop-Agent` |
| 2 Run location during Pilot 1 week | Open. Plan assumes the EliteDesk with the memory-derived slot count; the widget runs in Michael's session either way |
| 3 Payment path | **Answered**: API keys. Stage 1 ran on the Claude subscription through `claude_headless` and shared Michael's session window (hit 2026-10-08 11:55 CDT); an API-key executor is the Stage 2 priority. No key is on the EliteDesk yet |
| 4 Integration authority | Open. Stage 1 uses draft PR only |
| 5 Owner-decision taxonomy | **Answered** by directive point 9 |
| 6 PR #67 in CAOSCare | Open. This platform supersedes its runtime half; directive point 11 says do not disturb CAOSCare, so closing or keeping #67 is Michael's call there |
| 7 `PROJECT_STATE.md` authorship | Open; Stage 2 |
| 8 Remote access | Open; Stage 2 |
| New: BUILD authorization | Open; gates Stage 1 |
| New: Deal Sniffer repository | Open; confirm `michael-business-os` or name the repo |
