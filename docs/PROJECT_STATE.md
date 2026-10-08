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

## 2026-10-08 — Stage 1 acceptance: first real CAOSCare task end to end

- **Agent/tool:** Desktop-Agent control plane (user service on the EliteDesk) launching a Claude Code headless worker; coordinator Claude Code (Fable 5.1).
- **Branch/ref:** `build/stage-1` (PR #2).
- **Task:** `add-the-desktop-agent-project-pa-81d202`, goal `g-856960c4`, project caoscare, `config` scope, model floor `cloud_strong` → `claude-sonnet-5-5`. Objective: add `.agentproject/project.yaml` + REPO_MAP entry + PROJECT_STATE entry.
- **Result (control plane's own observation):** worker claim DONE at `62fa2159f72b234268459943bd1ff1b2ddc6a5bb` (8 turns, $1.82); verifier passed in a clean checkout: `backend/scripts/run_backend_tests.sh` exit 0, 338 passed / 31 skipped / 13 deselected; files within owned area; branch `agent/add-the-desktop-agent-project-pa-81d202` pushed, ls-remote SHA equal to local head; draft PR https://github.com/caosos/CAOSCARE.COM/pull/105. Receipt chain: worker `unverified` → verifier `verified` → integrator `verified`. Stages observed on the panel: READING → BUILDING → TESTING → REVIEWING → INTEGRATING → DONE, from events only.
- **Before it passed (recorded truthfully, 6 failed/blocked tasks remain in the store):** run 1 (3 attempts) died at launch because `systemd-run --scope` rejects `Nice=`; run 2 (3 attempts): attempts 1–2 on the cheap class failed with "Prompt is too long" because CAOSCare's `CLAUDE.md` loads its 805 KB `PROJECT_STATE.md`, attempt 3 (strong class) died on asyncio's 64 KB stdout line limit and the worker was left running. Fixes: no `Nice=` property, 64 MB line limit, kill-on-error, `min_model_class` per project, per-task `bin/run_tests.sh` the worker is explicitly allowed to run, broader read-only command allowlist (deny list unchanged), prompt says use Write/Edit not heredocs and read only the tail of the state file.
- **Known spec error of mine:** the contract named `backend/actor_context.py` as a shared contract path; the file is `backend/routes/actor_context.py`. The worker reported the mismatch instead of guessing. Fixed in `config/projects/caoscare.yaml`; the copy inside PR #105 needs the same one-line edit (noted on the PR).
- **What is blocked:** nothing. Runs 4 (red regression tests for the parked menu-parser cosmetics, `max_attempts: 1`, expected to fail verification and be shown as failed) and 5 (the fix) are queued.
- **Next safe step:** record runs 4 and 5; then the widget check, which needs Michael in his desktop session (`docs/WIDGET_SETUP.md`).

## 2026-10-08 — Stage 1 acceptance run 4: a failing run shown truthfully

- **Task:** `write-red-regression-tests-expec-558993` (goal `g-4631ed49`, caoscare, `tests_only`, `max_attempts: 1`, strong class by project floor). Objective: red regression tests for the parked menu-parser cosmetics (acceptance report 2026-10-08 item 3), parser untouched.
- **Result:** worker claim DONE at `b6e658b75e3225c078ca1f750feefb78b8fed7bf` with "3 failed, 396 deselected (exit 1)" (red by design, $1.93); verifier ran `backend/scripts/run_backend_tests.sh -k menu_parser_cosmetics` in a clean checkout → exit 1 → `VERIFY_FAILED`; task FAILED → BLOCKED after the single allowed attempt; nothing pushed, no PR. Receipts: worker `unverified`, verifier `failed`. The branch and its red tests remain in `~/Desktop-Agent-work/write-red-regression-tests-expec-558993/repo` for the fix task to reuse if the coordinator later chooses to.
- **Defect found and fixed:** the verifier detail (test output) was dropped when FAILED became BLOCKED, so the panel could not show why. Fixed in `scheduler._fail`.
- **Scheduler hold observed:** run 5 (`fix-the-three-parked-menu-paste--8b877c`) sat READY with `budget_slots = 0`: $3.75 spent in the last hour against the configured $5/hour cap and $2 expected per worker-hour. The hold reason is now shown in `/v0/state` and on the panel. Spending caps were not raised; the slot frees when the earlier spend ages out of the window.
