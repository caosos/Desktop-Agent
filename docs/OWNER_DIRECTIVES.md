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
