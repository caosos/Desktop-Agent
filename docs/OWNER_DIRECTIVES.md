# Owner directives (append-only)

Michael's product direction for Desktop-Agent, dated, in his own terms.
This file is the source of truth for *what Michael asked for*. Design and
implementation documents cite it; they do not restate it.

---

## 2026-10-08 — Autonomous Execution Directive

Relayed by Michael from his planning conversation. Recorded as given.

> Keep working continuously until the approved project objectives are
> complete or a genuine owner/external dependency blocks progress.

1. Operate as a persistent autonomous coordinator. Do not wait for Michael
   to say "continue" between approved tasks.
2. Dynamically spawn as many bounded, disposable worker agents as can
   productively run, based on workload, hardware capacity, model/API
   limits, cost budget, and dependency conflicts. No arbitrary permanent
   two-worker limit.
3. Maximize useful parallelism, not agent count. Avoid duplicated work,
   competing file edits, and unnecessary token spending.
4. Maintain a durable queue. Automatically derive next tasks from approved
   requirements, failing tests, verified defects, and unmet acceptance
   criteria. Do not invent cosmetic busywork.
5. Every worker receives a bounded task contract, isolated workspace,
   scoped permissions, acceptance tests, and resource limits.
6. Workers report structured events, complete their assignments, and exit.
   The persistent coordinator verifies results, integrates eligible
   changes, records receipts and provenance, and dispatches the next
   useful tasks.
7. Automatically detect stalled workers, retry recoverable failures, and
   escalate difficult tasks to more capable models when justified.
8. Preserve: No action without a receipt. No receipt without provenance.
9. Request approval only for genuine owner decisions, credentials,
   destructive operations, production deployment, policy changes, or
   unavailable external dependencies.
10. Make continuous progress visible in the dashboard, including workers,
    tasks, tests, costs, failures, receipts, and blockers.
11. Desktop-Agent is a separate project. Do not disturb CAOSCare or Deal
    Sniffer implementation.
12. Remain in DESIGN mode until Michael explicitly authorizes BUILD.
    Prepare the architecture and implementation plan.

**Goal:** Michael gives one instruction; the platform coordinates its own
agents, verifies outcomes, and continues without terminal babysitting.

**Locked principle:** Don't maximize the number of agents. Maximize the
amount of verified work completed per dollar and per hour.

**Distinction Michael drew:** this is a requirement on the platform. It
does not mean the current interactive Claude terminal can spawn unlimited
workers or run indefinitely; that terminal keeps its own execution limits.

### Product vision stated with the directive

"An agent on my desktop, like a little widget I can talk to," connected to
a cloud model through an API key first; more hardware and our own models
later. Staged:

1. **Desktop widget + cloud AI.** A small floating "Aria" interface with
   voice and text. It understands instructions and talks to the control
   plane.
2. **Autonomous agent coordination.** Aria delegates tasks, spawns bounded
   workers, tracks progress, controls spending, verifies results, and
   continues automatically.
3. **Local models and upgraded hardware.** Add our own models when the
   economics make sense. The interface and control plane stay the same;
   only the providers change.

Widget concept (concept only, not a running application): ARIA · Ready ·
"What would you like me to work on?" · talk or type · ACTIVE PROJECTS with
a status chip each (CAOSCare, Deal Sniffer, Desktop Agent).

"The widget is just the interface. Behind it is the persistent control
plane that manages agents, tools, memory, project state, permissions, and
receipts. You shouldn't have to know which model is doing the work or
which terminal it runs in unless you want to inspect it."

**First things first:** finish designing the small desktop interface and
its connection to the control plane. Then prove that Michael can tell Aria
to complete one task and watch it finish without babysitting anything. No
hardware purchase or local models needed for that.

### What this directive changes in the existing design

- Supersedes the fixed "max two workers" in the architecture review
  (§1.3, §5, §11) with a resource-, dependency-, cost- and conflict-driven
  scheduler. See `IMPLEMENTATION_PLAN.md` §4.
- Adds the desktop widget as the primary Michael-facing surface; the web
  instrument panel remains the inspection surface.
- Answers review §12 decision 3: model usage is paid through API keys.
- Supplies the owner-decision taxonomy (point 9), answering review §12
  decision 5.
- Confirms review §12 decision 1 (separate repository, `caosos/Desktop-Agent`).
- Adds a gate: no implementation until Michael says BUILD.

---

## 2026-10-08 (later) — Owner Expectations & Operating Directive

Received from Michael with the command "BUILD. Keep busy until I'm needed." Recorded in summary; the full text is Michael's.

1. **What is being built:** Aria as Michael's persistent desktop assistant and AI operating system: a small Linux desktop widget, voice or text, that understands a request ("Build this", "Fix this", "Show me what my agents are working on", "Finish this project", "Find out what's blocking progress", "Handle this and let me know when you actually need me"), identifies the project, organises and assigns work, monitors, verifies and continues. No terminals, no restarting agents, no babysitting.
2. **One assistant managing everything:** one dashboard across Desktop-Agent, CAOSCare, Deal Sniffer/MichaelOS and future projects: projects and agents, what each is doing and with which model, last activity, health, stalls, failures, tests, changes, commits, verified results, real costs and remaining budgets, blockers, decisions, next actions. Detect stopped, stuck, duplicating or wasteful agents; investigate, recover, reassign or escalate.
3. **Automatic model selection (extremely important):** never ask Michael to pick a model. Choose the least expensive model capable of the task: Luna-class for extraction, classification, summaries, repetitive work; Sol-class for normal development, coding, research, debugging; Astra-class only when justified. Capability classes, not vendor restrictions. Consider complexity, capabilities, context, pricing, quotas, previous failures and results; escalate intelligently on failure. Measure verified work per dollar and per hour. Track real costs; never invent estimates.
4. **Autonomous execution:** persistent; as many bounded workers as resources, cost, permissions and dependencies allow; no arbitrary two-worker cap; workers do assigned work, test, report, exit; the control plane schedules the next useful task; no "continue" prompts; no busywork.
5. **Bigger than coding:** eventually operate the computer through governed tools (files, software, websites, documents, spreadsheets, business workflows, desktop apps). Voice and text lead into the same execution system. Widget = interface; control plane = execution and supervision.
6. **Hardware and model independence:** existing cloud services and owned Linux hardware first; no GPU prerequisite; possible RAM/storage upgrade; later local models, more machines, distributed processing; design for it now; no single-provider dependency.
7. **Permanent rules:** "No action without a receipt. No receipt without provenance." "Capture everything. Execute one thing. Finish it. Then move." Independent verification; traceability to instruction, actor, files/systems, tests, outcome. Project truth survives lost conversations, expired sessions, replaced agents and restarts; nothing critical lives only in chat memory.
8. **Michael's time:** reasonable technical choices made independently within approved boundaries; involve him only for owner decisions, credentials, unauthorised spend, destructive actions, significant governance changes, production deployment approval, unavoidable external dependencies. When a decision is needed: explain, recommend, ask one question.
9. **Boundaries and coordination:** Desktop-Agent is separate from CAOSCare and Deal Sniffer; it may manage them later but must not silently change their implementations. **The ChatGPT Work agent is the sole active Desktop-Agent development coordinator**; other agents contribute research, review or assigned bounded work without competing implementations or conflicting project state. Use the existing repository, review, plan and history; do not restart solved work without evidence.
10. **Standard for success:** tell Aria what is needed, walk away, and on return see what got done, what was independently verified, what remains, what failed and why, cost, what is next, and whether a decision is needed.

### Acknowledgement and gap analysis by the Claude Code coordinator (this session)

Acknowledged. From this point this session is **not** the development coordinator; it finishes the Codex-adapter piece it had in hand, leaves project state consistent, and takes only assigned bounded work, research or review from the ChatGPT Work coordinator or Michael.

Measured against the directive, what exists today (`build/stage-1`, `build/stage-2`) and the genuine gaps:

| Expectation | State today | Gap |
|---|---|---|
| Widget, voice/text → execution | GTK4 widget code with direct-command and conversational modes; push-to-talk optional | Not yet shown on Michael's desktop; no Anthropic key on the box for conversational mode |
| Understand request → identify project → organise → assign | One instruction → one task contract (template); project named explicitly | **Planner missing**: natural-language request → project identification → several contracts with dependencies |
| One dashboard across projects | Panel + API: projects, workers, model, last activity, tests, files, receipts, blockers, next, costs, hold reason | Health/stall shown only as kill events; no duplicate-work detection; MichaelOS not onboarded (repo name unconfirmed) |
| Automatic model selection, least expensive capable, learns from failures | Class ladder (`cloud_cheap` / `cloud_strong` / `cloud_max` ≈ Luna / Sol / Astra), task-type default, per-project floor, escalate-on-failure, per-adapter maps | **No evaluation of complexity/context per task; no memory of prior outcomes per task type; no quota awareness across providers** |
| Real costs, no invented estimates | Claude headless cost from its own `total_cost_usd`; Codex cost recorded as unknown with token counts | Subscription executors have no dollar figure; an API-key executor would make costs exact (owner item: key) |
| As many workers as resources allow | Admission formula implemented; ceiling configured at 1 for Stage 1 | Ceiling must be raised by config once two concurrent runs are proven; feedback loop (verified/$ and /hour) not yet computed |
| Verification and receipts | Enforced in code: worker claims `unverified`; verifier/integrator `verified` or `failed`; clean-checkout tests; ls-remote check | — |
| Project truth survives sessions | SQLite + JSONL + git; project packages point at repo truth | — |
| Governed desktop tools beyond coding | Not started (by plan, Phase 3) | — |
| Provider independence | Two executors (Claude Code, Codex), both CLI/subscription based | No direct-API or local-model executor yet |
| Only genuine owner decisions | Decision inbox exists in API/widget but nothing files decisions yet | Planner/scheduler should file decisions instead of BLOCKED-with-reason |
