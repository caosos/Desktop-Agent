# AGENTS.md — Desktop-Agent working rules

This file is the mandatory entry point for any human or AI agent working in
this repository. Read it first, then `START_HERE.md`.

Desktop-Agent is Michael's build/desktop agent platform: a persistent
control plane that turns a goal in plain language into bounded tasks, runs
them in disposable sandboxed workers, verifies the result, records receipts
with provenance, and shows truthful progress on an instrument panel. It
supervises workers across projects (CAOSCARE.COM is the first) and, later,
navigates desktop screens as a second set of hands.

The working rules below are **inherited from the CAOSCare project**
(`caosos/CAOSCARE.COM`, `AGENTS.md`) and apply here unchanged unless this
file says otherwise. Care-domain content from that file (product identity,
care safety rule, preserve list) does not apply here; those stay in CAOSCare.

## Operating mode: inspect first

Do not code, redesign, or rewrite claims until you know:

1. the exact branch/ref you are on
2. which files exist here (this repo is small; read them)
3. whether the task is design, documentation, control plane, worker
   adapter, sandbox, UI, or project onboarding
4. what the architecture review and `docs/PROJECT_STATE.md` say is decided,
   pending, or superseded

Keep distinct at all times: repository truth · runtime truth · test evidence
· Michael-provided direction · inference · planned future capability.

## Receipt and provenance law

**No action without a receipt. No receipt without provenance.**

Every meaningful action or state change by a human, agent, scheduler,
provider, adapter, or external integration must leave durable evidence
linked to its origin: who or what initiated it, why it was authorised, what
changed, what executed it, and what evidence proves the result. A worker's
self-report ("done", "tests passed") is never proof. Coding work needs
branch, diff, test output and commit evidence. A result label is one of
`verified` (observed by the control plane or Michael), `unverified`
(self-report), `simulated`, or `failed`.

This law is also the platform's own design invariant; see
`docs/ARCHITECTURE_REVIEW_2026-10-08.md` §8.

## Bounded workers

Every unit of work is one task, one branch, one worktree, tests, a commit, a
receipt, a handoff, then exit. No long-lived agent sessions that accumulate
context. Workers never push to `main`, never merge, never deploy, never hold
provider or GitHub secrets.

Concurrency is not a fixed number. The platform's scheduler sizes it from
hardware capacity, cost budget, provider limits and edit conflicts
(`docs/IMPLEMENTATION_PLAN.md` §4; `docs/OWNER_DIRECTIVES.md` 2026-10-08).
The measure is verified work per dollar and per hour, not agent count.
Until the scheduler exists, humans launching workers by hand keep to two at
once. Idle is better than destructive parallelism.

## Change discipline

- Small, bounded changes. One source of truth per rule, policy, constant or
  prompt; point at it, do not copy it.
- Implementation files stay under ~300 lines; 300 is the split/review
  threshold. Split by coherent responsibility, never by line count. Files
  that are mostly information (docs, schemas, reference data) are exempt.
  Report line counts of every created or materially changed implementation
  file before finishing. A worker may not claim DONE while its change newly
  creates or worsens an unjustified >300-line implementation file.
- No abandoned duplicate implementations, mystery files, dead routes or
  contradictory documentation. Retire the old thing deliberately after the
  replacement is verified.
- Documentation describes reality. Plans are not implementation;
  compilation is not verification. A feature is complete only when the
  intended path works and its acceptance test passes.

## Branch hygiene

- One branch per bounded task: `agent/<task>`, `feature/<name>`,
  `fix/<name>`, `docs/<name>`, `chore/<name>`.
- `main` is the integration branch, not a workspace. Nothing lands on
  `main` without Michael's approval until an integration policy is ratified
  (review §12, decision 4).
- Inspect existing branches before creating one. Classify before deleting.

## Stop conditions

Stop and report before acting if:

- the change needs a decision that is genuinely Michael's: owner/product
  decisions, credentials, destructive operations, production deployment,
  policy changes, or unavailable external dependencies (the taxonomy in
  `docs/OWNER_DIRECTIVES.md` 2026-10-08, point 9)
- implementation is requested while the project is in DESIGN mode (Michael
  authorises BUILD explicitly)
- a destructive or irreversible action is requested (deleting worktrees or
  branches with unique work, force pushes, touching another project's live
  services)
- a credential, external account, or piece of hardware is missing
- the task would touch CAOSCare's live room node (`:3000`, `:8092`, Mongo
  `caoscare`) or any production system
- evidence contradicts a claim in the docs

## Continuous progress

Michael's standing preference is continuous useful progress: never idle
while approved work remains, prefer a working testable step over prolonged
speculation, use the most economical capable model and escalate only when
justified, and derive the next bounded task from the acceptance gap when the
queue runs dry. Do not invent cosmetic busywork.

## Escalation and reboot-continuity protocol

A blocked worker does not become a silent idle worker.

- Use receipt states `DELIVERED`, `ACK`, `WORKING`, `DONE`, `BLOCKED`, and `NEED`. The local watchdog must detect an unacknowledged or stalled assignment within five minutes and either hand off the next eligible task or produce a truthful escalation receipt.
- A `NEED` receipt must name the project and task, timestamp, exact blocker, classification (`NEED_SERVICE`, `NEED_ACCESS`, `NEED_PERMISSION`, `NEED_HOST_ACTION`, or `NEED_OWNER`), exact requested action or one copyable command, impact, evidence, compatible work continuing, and next check time.
- Do not report a problem without the proposed fix. Service and host failures include the restart/recovery action and health verification; access failures identify the exact missing credential or permission.
- Continue other compatible authorized work while a NEED is pending. Ask Michael only for a genuine owner decision, unavailable credential/access, spending/provider commitment, destructive or irreversible action, physical action, or conflicting instruction.
- Existing project control paths remain canonical: Desktop Agent Issue #3; CAOSCare Issue #129; Deal Sniffer's existing liaison/READY_QUEUE. Do not create a duplicate queue or coordinator.
- Reboot recovery must be proven, not assumed: enabled service or session supervisor; loaded revision; process identity; expected ports/health checks; coordinator reattachment; queued-task replay; and a timestamped recovery receipt.
- Cloud supervisors are hourly reconciliation and PR watches are event-driven GitHub reconciliation. They do not replace the local five-minute watchdog.
- Mirror actionable NEED receipts to Slack `#all-ask-aria` once the ChatGPT Slack app is installed in that channel; until verified, the existing issue/liaison plus hourly supervisor is the delivery path.

## ETA-driven checks and exception alerts (owner operating addendum, 2026-10-11; applies to every project)

Adopted in code, not only in a prompt: `desktop_agent/control/checks_receipt.py` (schema, notification filter), `checks.py`
(the due-check pass inside the existing local watchdog), `checks_api.py` (`GET/POST /v0/checks`). No second watchdog,
queue, worker or monitor exists or may be created for this.

1. **Every ACK / WORKING / BLOCKED receipt** a coordinator or worker posts carries these lines (`key: value`, one per line):
   `project`, `task` (bounded task id), `responsible` (coordinator or worker), `stage`, `evidence` (what was reviewed),
   `eta`, `eta_basis`, `next_check_at`, `acceptance` (the test or completion evidence expected), `blocker` (`NONE` or exact).
2. **The ETA comes from the real scope**: `eta_basis` names the work, tests, browser verification, deployment steps and host
   conditions it is derived from; a generic phrase is rejected. `eta` and `next_check_at` are ISO-8601 with the
   America/Chicago offset in force at that instant (`2026-10-11T20:15:00-05:00`); anything else is rejected.
3. **The local watchdog owns the timer.** Reading a valid receipt on the issue arms a check at the exact `next_check_at`; a
   newer receipt replaces it; an invalid receipt arms nothing and is recorded with its problems.
4. **At `next_check_at`** the watchdog inspects the current process, receipts, repository state and loaded service state
   (no model) and publishes exactly one of **WORKING** (evidence, revised ETA, new `next_check_at`), **DONE** (the coordinator's
   receipt carries commit, tests, browser evidence, loaded revision; the check names the next eligible queue row) or
   **BLOCKED** (exact blocker and required action or command, impact, compatible work, next check time).
5. **Finishing early** means an immediate DONE receipt; the check closes at once and names the next eligible item. Do not wait.
6. **A missed ETA or `next_check_at` is an exception**, recorded with provenance. Never claim a worker stopped unless host
   evidence proves it (the registry shows the session absent); otherwise write `APPEARS STALE — HOST STATE UNVERIFIED`.
7. **Michael is notified only for** a DONE that needs his acceptance; NEED / BLOCKED; a missed ACK, ETA or check time; a
   failed test, delivery, deployment or recovery; a genuine owner decision; a material cost or authority boundary. Not for
   routine healthy progress, build authority already granted, or unchanged state (`notify_decision` in `checks_receipt.py`).
8. **The cloud supervisor is the hourly fallback**, not the timer. Every hourly run shows: current bounded task and queue
   stage; latest exact receipt and commit; tested versus loaded state; last timestamped host observation or UNKNOWN; ETA and
   `next_check_at`; blocker; one next action. Cloud GitHub activity is not proof that a host process is alive.
9. **Projects stay isolated**: never edit, dispatch, supervise or recover another project's work; route misdelivered work to
   its project and keep it quarantined until the intended recipient is verified.
10. Governing rule, unchanged: **no action without a receipt, no receipt without provenance.** Every check event and receipt the watchdog writes carries its evidence.

Example receipt block:

```text
project: desktop_agent
task: ETA-driven checks and exception alerts
responsible: coordinator caoscare-1-8c
stage: implementing the watchdog check pass
evidence: queue row, watchdog tick, intake read-back, existing tests
eta: 2026-10-11T21:15:00-05:00
eta_basis: schema + timer + six test groups + direct gate + one restart + first live check
next_check_at: 2026-10-11T20:15:00-05:00
acceptance: tests prove schema, timer, three outcomes, early DONE, missed-check exception, notification filter
blocker: NONE
```

## One hard daily budget: Daily maximum paid API spend (owner directive 2026-10-11 20:57 CDT; `da-8708eb5ec7`)

Hardcoded in `desktop_agent/control/paid_budget.py` (default `$0`), enforced at the single paid-call choke point `LLM.complete`, shown in the panel bar and stated in Ask Aria's instructions; tests in `tests/test_paid_budget.py`.

1. There is one owner setting, **Daily maximum paid API spend**. Default $0. Only the owner changes it (panel or `POST /v0/paid-budget`, receipted).
2. It bounds actual paid API spend only. Subscription-included Claude CLI/Codex usage, local and cached work are **not** spend: never call them spend, cost, charged or a cap in owner-facing UI or messages, and never show per-message or aggregate token-equivalent dollars there. Such figures are internal diagnostics, shown only behind technical evidence and labelled `estimate; not billed`.
3. Provider billing receipts are the only authority for actual paid spend; a provider-reported usage figure is shown as such until a billing receipt covers it.
4. Route subscription/local first, then the cheapest capable authorized paid route. At the maximum, make no further paid call, continue compatible subscription/local work, record one NEED receipt, and notify Michael only if the limit blocks necessary work (name the task and the smallest decision).
5. No model call, worker, relay, retry or background task may bypass it: every control-plane Claude CLI runs with no API key, auth token or gateway (`subscription_env`); workers hold no provider key; a paid HTTP call needs a ledger hold keyed by call id, so restart and replay cannot reset or double-count the day.

## Handoff to Michael

End every work block by committing and pushing to the authorised branch,
then give Michael one reference block he can paste to Aria:

```text
REFERENCE: <full commit SHA>
BRANCH: <branch name>
GITHUB: https://github.com/caosos/Desktop-Agent/commit/<full SHA>
RESULT: what changed, in 2–4 sentences
VERIFICATION: tests/build performed and their actual results
RECEIPT: originating task, files/state changed, evidence of the result
STATUS: complete | blocked | still in progress
NEXT: the one next action
```

Verify the SHA on GitHub (`git ls-remote`) before giving the block. Work
that exists only locally is reported as **LOCAL ONLY** and is not complete.

## Project state

`docs/PROJECT_STATE.md` is append-only. **Bounded workers launched by the
platform do not edit it**: the control plane appends one entry per verified
task at integration time (`state_entry_by: control` in this project's
descriptor), which keeps a single writer and avoids merge conflicts between
concurrent workers. Humans and the coordinator still append dated entries for
work done outside a bounded task (date, agent/tool, branch, what changed, what
was verified, what is blocked, next safe step). Never erase history; label
corrections.
