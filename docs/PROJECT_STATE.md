# Desktop-Agent — project state (append-only)

Dated entries, newest last. Never erase; label corrections.

## 2026-10-08 — repository created, architecture review landed

- **Agent/tool:** Claude Code (Fable 5.1) on the EliteDesk, acting as
  independent architecture reviewer.
- **Branch/ref:** `main` (first content commit after Michael's initial commit
  `eedbbbf`).
- **What changed:** `AGENTS.md` (working rules inherited from CAOSCare),
  `START_HERE.md` (bootloader), `CLAUDE.md` (boot pointer),
  `docs/ARCHITECTURE_REVIEW_2026-10-08.md` (the review), this file.
- **What was verified:** the review's host facts were read from the machine
  (`nproc`, `free`, `lscpu`, `which`); the prior control-plane attempt
  (`~/CAOSCARE-AGENT-CONTROL`, branch `pilot/agent-control-plane`) and PR
  #67 were inspected; bounded-worker prompt/output pairs under
  `~/CAOSCARE-BOUNDED/` were read. Component claims come from public
  documentation listed at the end of the review. No code written, nothing
  executed beyond read-only inspection.
- **What is blocked:** Phase 0 waits on Michael's decisions in review §12,
  chiefly run location during Pilot 1 week and the payment path for model
  usage.
- **Next safe step:** Michael answers §12; then a bounded task defines the
  Phase 0 slice's acceptance test and file layout.

HANDOFF CAPSULE
- Objective:        design accepted → build Phase 0 vertical slice
- Branch:           main
- Lane / ownership: whole repo (nothing else exists yet); never touch CAOSCare live services
- Last proven state: documentation only; review published
- Commits:          see git log
- Runtime state:    none for this project
- Unresolved proven defects: none
- Product invariants that matter here: receipt law; fresh bounded workers; project truth stays in the managed project's repo
- Do NOT change:    CAOSCare repo files from this project
- Next safe action: wait for §12 answers, then write the Phase 0 task contract

## 2026-10-08 — Autonomous Execution Directive recorded; implementation plan written

- **Agent/tool:** Claude Code (Fable 5.1) on the EliteDesk.
- **Branch/ref:** `docs/directive-and-implementation-plan` → PR into `main`.
- **What changed:** `docs/OWNER_DIRECTIVES.md` (new, append-only: Michael's
  12-point directive, locked principle, staged product vision);
  `docs/IMPLEMENTATION_PLAN.md` (new: system shape, Aria desktop widget
  design, control-plane API v0, dynamic concurrency scheduler replacing the
  fixed two-worker cap, stall/retry/escalation, stages with acceptance,
  repository layout, decision status); `AGENTS.md` (concurrency rule now
  scheduler-driven; owner-decision taxonomy from the directive; DESIGN-mode
  stop condition); `START_HERE.md` (status, reading order, decisions, next
  step).
- **What was verified:** host facts for the widget design were checked on
  the EliteDesk: the GNOME session belongs to user `michaelos` while repos
  and CLIs belong to `caoscare-1`; PyGObject with GTK 4.6 is importable;
  `xdotool` present, session type unconfirmed. No repository named Deal
  Sniffer exists under `caosos`; `michael-business-os` is the closest
  match (unconfirmed). Documentation only; nothing executed or built.
- **What is blocked:** implementation, by design, until Michael says BUILD.
- **Next safe step:** Michael reviews the plan and either authorises BUILD
  (Stage 1 starts in the listed order) or amends the plan.

HANDOFF CAPSULE
- Objective:        DESIGN complete enough to start Stage 1 on BUILD
- Branch:           docs/directive-and-implementation-plan (PR to main)
- Lane / ownership: whole repo; never touch CAOSCare or Deal Sniffer implementation
- Last proven state: documentation only; no runtime
- Commits:          see PR
- Runtime state:    none for this project
- Unresolved proven defects: none
- Product invariants that matter here: receipt law; bounded disposable workers; scheduler-sized concurrency measured as verified work per dollar and hour; widget never does work; desktop Aria ≠ resident Aria
- Do NOT change:    CAOSCare repo; anything under ~/CAOSCARE-*
- Next safe action: wait for BUILD; then Stage 1 step 1 (store + events + receipts)

## 2026-10-08 — BUILD authorised; Stage 1 control plane built (steps 1–6, 8)

- **Agent/tool:** Claude Code (Fable 5.1) on the EliteDesk, acting as coordinator and sole worker (the platform cannot yet launch its own workers for its own repo).
- **Authorization:** Michael's comment on PR #1: "BUILD. Keep busy until I'm needed." PR #1 merged to `main` (`13ae56a`).
- **Branch/ref:** `build/stage-1` (Stage 1 integration branch; PR to `main` when the acceptance run is recorded).
- **What changed:** `desktop_agent/control/` — events + stage derivation, SQLite/JSONL store, receipts (law enforced in code), contracts, project loader, runtime config, sandbox (systemd-run scope + bwrap, per-worker HOME with only the Claude credential), per-task git workspaces (local clone + remote fetch; the integration checkout is never touched), claude_headless adapter (stream-json → events), launcher, verifier (clean checkout, owned-area and shared-contract check, acceptance tests), integrator (push + ls-remote check + draft PR), router (class → model, escalation ladder), scheduler (admission formula from IMPLEMENTATION_PLAN §4, stall kill, retry → escalate → BLOCKED), service, FastAPI v0 API with SSE; `panel/index.html` instrument panel; `config/` runtime, CAOSCare project descriptor, worker prompt template; `tests/`.
- **What was verified:** `19 passed, 1 warning in 4.78s` (unit + end-to-end pipeline with a fake executor against a local bare remote: good path DONE with verified receipt chain and remote SHA check; failing tests → retry → escalation → BLOCKED after attempt 3; shared-contract violation fails verification; worker BLOCKED claim; API auth/idempotency/state/control/receipts/events replay). Host checks: bwrap + systemd-run work for this user; Claude Code 2.1.293 runs headless from a per-worker HOME (one real $0.27 probe run); stream-json shapes recorded in the adapter docstring.
- **Deviations from the plan, with reasons:** workspaces are per-task local clones rather than `git worktree` so a worker can never reach the integration checkout's `.git`; the governed MCP tool server is deferred because the worker's claim is its final message (parsed into CLAIM_WRITTEN + unverified receipt) and no worker-side tool needs the control plane yet; CAOSCare's project descriptor lives in `config/projects/caoscare.yaml` until CAOSCare carries its own.
- **What is blocked:** nothing for the next step.
- **Next safe step:** install the control plane as a user service, run the Stage 1 acceptance against CAOSCare (one docs/config-scope task), then build the Aria widget (step 7).
