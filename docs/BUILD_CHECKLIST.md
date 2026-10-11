# Desktop Agent — Living Product Build Checklist

**Snapshot:** 2026-10-11 00:06 CDT (GitHub evidence through 2026-10-11 04:52 UTC). **Scope:** caosos/Desktop-Agent only. This is a planning and reporting record, not a worker dispatch, deployment, provider change, or release approval.

**Product finish line:** Michael talks or types to Aria on his desktop; Aria can manage the three projects, use approved desktop/browser/SSH tools, derive and run useful bounded work with economical model choice, show live progress and receipts, and keep going while Michael is away. The owner-facing screen must remain understandable. The full finish line has **not** passed acceptance.

**Evidence rule:** no action without a receipt; no receipt without provenance. A commit proves code exists, a test proves its tested behavior, a host observation proves what ran there, and Michael's use proves the owner experience. Do not check a whole feature DONE from a worker claim, a passing unit test, or a draft PR. The canonical event log is [PROJECT_STATE](PROJECT_STATE.md); [ASK_ARIA_PLAN](ASK_ARIA_PLAN.md), [BLUEPRINT](BLUEPRINT.md), [RESEARCH_BRIEF](RESEARCH_BRIEF.md), and [Issue #3](https://github.com/caosos/Desktop-Agent/issues/3) carry the detailed contracts and receipts. If this snapshot conflicts with a newer receipt, update it and link the correction.

## At a glance

- **Current milestone:** Phase 2/6, make Ask Aria reliably usable from Michael's screen. Typed Ask Aria and the local-source research lane have live acceptance; microphone timing and the newly observed first-Send loss remain open.
- **Newly finished since the prior checkpoint:** [PR #14](https://github.com/caosos/Desktop-Agent/pull/14), [PR #15](https://github.com/caosos/Desktop-Agent/pull/15), [PR #16](https://github.com/caosos/Desktop-Agent/pull/16), and [PR #17](https://github.com/caosos/Desktop-Agent/pull/17) are merged. PR #16's merged gate was **174 passed, 1 skipped** and Michael's exact formerly failing typed question answered live. The first confirmed Ask Aria research job reached VERIFIED, its checker exited 0, and its report/PDF were published; see [Issue #3 receipt 6105583357](https://github.com/caosos/Desktop-Agent/issues/3#issuecomment-6105583357).
- **Integrated but awaiting live confirmation:** [3e14ead](https://github.com/caosos/Desktop-Agent/commit/3e14eadd7c84724727df59a1037a7be2579fb365) unwraps a one-level JSON-wrapped model reply; the recorded gate is **176 passed, 1 skipped**. [fa0291b](https://github.com/caosos/Desktop-Agent/commit/fa0291b1851babe7aba9f5fd2785f19503a4cfb0) records the research acceptance and harness fixes in PROJECT_STATE. No post-commit host load/owner acceptance is established by GitHub alone.
- **Open reliability observation:** the first Send after a fresh page load within about one minute of a service restart was lost twice in Chrome; no request reached the store and the cause is unknown. Reopening and sending again worked. This is reproduced evidence, not a diagnosed fix.
- **Live process and worker status now:** **UNKNOWN from this GitHub check.** The latest host receipt proves the earlier research run and loaded harness fixes at that time; it does not prove a process or worker is currently running.
- **Next eligible bounded task:** reproduce the lost-first-Send path with browser/network/store instrumentation, add a failing regression where possible, and fix only the proven cause. This checklist update does not dispatch that work.
- **Needs Michael:** one short microphone measurement (request → first interim → final) remains the Phase 2 owner acceptance. Web retrieval remains BLOCKED on its separate approval-packet decision; local-source research does not depend on it.
- **Owner constraint preserved:** work remains sequential, one accepted item at a time. No new Desktop Agent assignment or release is authorized by this update.

## Status key and timing

A row can have four evidence columns: **I** implemented in a branch or `main`; **T** a passing test run is recorded (the evidence names who ran it); **S** integrated/staging path accepted; **L** live path observed on Michael's host or by Michael. `✓` means evidence linked; `·` means still open; `n/a` means that gate does not apply. A partial phase can contain completed rows. No invented percentage.

All durations below are **focused engineering effort after the dependency is available**, not a calendar delivery date or a promise that a worker is running. Confidence is low where EliteDesk/owner interaction or provider access is needed. The tracker owner should replace these ranges with measured start/finish forecasts after a live checkpoint.

## Phase 1 — Core control plane and receipt law (foundation accepted)

**Milestone:** one bounded task moves through a sandboxed worker, independent verification, a receipt chain, and a reviewable PR; failures remain visible.

| Check | I | T | S | L | Evidence / remaining gate |
|---|:---:|:---:|:---:|:---:|---|
| Durable store, events, receipts, worker isolation, verifier, integrator, panel and API | ✓ | ✓ | ✓ | ✓ | [Stage 1 PR #2](https://github.com/caosos/Desktop-Agent/pull/2) merged; [PROJECT_STATE](PROJECT_STATE.md) records real CAOSCare runs with success and honest failure. |
| Pause/stop, restart recovery, event replay and scheduling failure hardening | ✓ | ✓ | · | · | Local Codex fixes on `fix/reliable-live-events`, 40 passed/1 host-specific deselected, remain **LOCAL ONLY** in this workspace; compare with newer `main` before porting. Some equivalent host behavior has since changed; do not claim the four commits integrated. |
| Graceful service stop and truthful repeated intake readback | ✓ | ✓ | ✓ | ✓ | [`269e042`](https://github.com/caosos/Desktop-Agent/commit/269e0422d8220eb7014acb2eb5e635ba42820b47), [host receipt](PROJECT_STATE.md) measured 5 s stop and unchanged store. |

**Remaining foundation work:** compare the old local fixes with current `main`, keep only uncovered defects. **Estimate:** 0.5–2 focused days, low confidence until comparison.

## Phase 2 — Usable Ask Aria on Michael's screen (CURRENT acceptance milestone)

**Milestone:** Michael asks in text or speech, can see Aria's actual action and source, can correct the transcript, and can watch an independently verified task finish without a terminal.

| Check | I | T | S | L | Evidence / next action |
|---|:---:|:---:|:---:|:---:|---|
| Ask Aria text, project-grounded status, clear blocked/owner answer | ✓ | ✓ | ✓ | ✓ | [a42f4e3](https://github.com/caosos/Desktop-Agent/commit/a42f4e399561bde394643af8014714705f2693df) answered the blocked-state question live; [PR #16 acceptance](https://github.com/caosos/Desktop-Agent/issues/3#issuecomment-6105490122) proves Michael's exact formerly failing typed question answered after integration. |
| Multi-step Ask Aria question avoids claude_cli maximum-turns failure | ✓ | ✓ | ✓ | ✓ | [PR #16](https://github.com/caosos/Desktop-Agent/pull/16), merge 8a553a0; merged-head gate **174 passed, 1 skipped**; exact question answered live in Chrome at 2026-10-10 23:36 CDT. |
| Dictation shows interim words promptly and final words remain editable | ✓ | ✓ | ✓ | · | PR #16 integrated the interim/final state changes and regressions. Real microphone request → first interim → final timing still requires Michael's browser; do not infer it from unit/browser automation. |
| One owner-confirmed bounded task completes with receipts from Ask Aria | ✓ | ✓ | ✓ | ✓ | [PR #17](https://github.com/caosos/Desktop-Agent/pull/17), worker 8103cfa, control-plane merge 7256bec; clean-checkout checker exit 0; [live acceptance receipt](https://github.com/caosos/Desktop-Agent/issues/3#issuecomment-6105583357). Report conclusions remain unverified beyond citation resolution. |
| JSON-wrapped model reply renders as the reply, not raw JSON | ✓ | ✓ | ✓ | · | [3e14ead](https://github.com/caosos/Desktop-Agent/commit/3e14eadd7c84724727df59a1037a7be2579fb365), recorded gate **176 passed, 1 skipped**. Integrated on main; no later live owner acceptance receipt found. |
| First Send after fresh page load/restart reliably reaches the store | · | · | · | · | Lost twice in Chrome within about one minute of restart; re-opening and resending worked. Cause unknown. Next bounded task: instrument browser request, API arrival and store write, then add a regression for the proven failure. |
| Panel is understandable while work runs; statuses and receipts are truthful | ✓ | ✓ | ✓ | ✓ | [adee737](https://github.com/caosos/Desktop-Agent/commit/adee73730536f76c6c4986ddbf0bb494a240d219) corrected blocked labels and was checked in Chrome. |
| Optional GTK widget and push-to-talk path from Michael's desktop | ✓ | ✓ | · | · | [Widget setup](WIDGET_SETUP.md) and unit tests exist; an actual display/voice acceptance receipt remains open. The browser panel is the usable owner surface meanwhile. |

**Current task:** no task is dispatched by this tracker. The next eligible bounded engineering task is the lost-first-Send reproduction/fix. **Dependency:** reproduce around page load/restart without conflating it with microphone latency. **Next checkpoint:** a failing regression or captured browser/API/store trace that identifies the layer, followed by a narrow fix and gate. **Estimate:** 0.5–1 focused working day, **low confidence** until the failure is isolated. Michael's microphone check is a separate short acceptance step.

## Phase 3 — Reliable autonomous build supervisor across three projects

**Milestone:** Aria derives the next eligible bounded task, supervises useful parallel work within budget, prevents duplicate/conflicting dispatch, recovers from interruption, handles approval/denial paths, and reports only actual owner decisions for a full supervised day.

| Check | I | T | S | L | Evidence / open gate |
|---|:---:|:---:|:---:|:---:|---|
| Planner, scheduler, two concurrent workers, Claude and Codex paths, receipt-backed verification | ✓ | ✓ | ✓ | ✓ | [BLUEPRINT](BLUEPRINT.md) and [PROJECT_STATE](PROJECT_STATE.md) record live runs; concurrency remains resource/budget/dependency-driven. |
| Three-project cards, project drilldowns, shared inbox and approval packet | ✓ | ✓ | ✓ | ✓ | [ASK_ARIA_PLAN](ASK_ARIA_PLAN.md), [PROJECT_STATE](PROJECT_STATE.md); Business OS liaison is manual-only in the latest host record, so full cross-account control is open. |
| Automatic next useful work, deduplication, cancellation/restart recovery, genuine owner escalation | ✓ | ✓ | · | · | Individual mechanisms exist, but the full-day unattended acceptance and queue derivation when READY is empty are not proven. Compare the LOCAL ONLY hardening before any port. |
| Current liveness and actionable notifications, not stale GitHub inference | ✓ | ✓ | · | · | Intake and panel exist; verify worker completion → coordinator acknowledgment → checklist/queue update → one owner alert on a real run. |

**Estimate to acceptance:** 5–10 focused working days after Phase 2's dependable command loop; **low confidence**, and longer if cross-account wake needs new permissions.

## Phase 4 — Research and useful artifacts through Ask Aria (LIVE local-source acceptance)

**Milestone:** Michael requests a real report, Aria states source and budget limits, one bounded worker researches, verifies citations, makes a usable PDF, and returns a link, progress and receipts in the conversation.

| Check | I | T | S | L | Evidence / open gate |
|---|:---:|:---:|:---:|:---:|---|
| Local-source research job, job state, deduplication, retry following, cancellation and checker | ✓ | ✓ | ✓ | ✓ | [PR #14](https://github.com/caosos/Desktop-Agent/pull/14) integrated research v1. Harness corrections [01ae8f5](https://github.com/caosos/Desktop-Agent/commit/01ae8f59a25fd2698600569bb3c619b288e2b7fb) and [5f47b67](https://github.com/caosos/Desktop-Agent/commit/5f47b67440114c249ba23d90f50fcb58236dfcc7) were exercised by the successful retry. [PR #17](https://github.com/caosos/Desktop-Agent/pull/17) reached VERIFIED; checker exit 0. |
| PDF render and authenticated report/PDF download | ✓ | ✓ | ✓ | ✓ | [347c162](https://github.com/caosos/Desktop-Agent/commit/347c162b24176a086d9c44a480d061e71464645e) integrated publication. [Live receipt](https://github.com/caosos/Desktop-Agent/issues/3#issuecomment-6105583357): PDF 82,884 bytes, SHA-256 f1e4475d…, 0.93 s; report and PDF returned 200 with auth and 401 without it. |
| Research content independently validates its cited conclusions | · | · | · | · | The checker verifies required structure and that citations resolve; it does not independently judge the report's conclusions. Independent source-line comparison remains open. |
| Web research with citations | · | · | · | · | Web retrieval is BLOCKED on the approval-packet decision and source policy in [RESEARCH_BRIEF](RESEARCH_BRIEF.md). Local-source work is accepted and need not wait. |

**Phase status:** the local-source workflow is implemented, tested, integrated and live-accepted. Web retrieval and independent semantic review remain separate open gates. No additional research job is authorized by this tracker.

## Phase 5 — Desktop, browser and SSH operator

**Milestone:** Aria can inspect and act on the approved desktop and SSH resources Michael names, show the proposed action and live outcome, honor pause/stop/change direction, and leave a receipt for each action.

- [ ] Define governed tool contracts, target allowlists, visible action preview and exact authority for desktop, browser, file and SSH operations.
- [ ] Build one narrow end-to-end desktop action and one SSH action with independent result observation and cancellation.
- [ ] Prove the path on Michael's EliteDesk and Deal Desk use case; verify mistakes, denials and recovery without claiming an action happened when it did not.
- [ ] Expand coverage only after each tool's acceptance. Current general desktop/SSH operator: **not verified**.

**Estimate:** several additional focused weeks, **very low confidence** until the first tool/host acceptance defines scope.

## Phase 6 — Economical provider routing and optional local models (cross-cutting)

**Milestone:** Aria chooses the cheapest capable worker from measured outcomes and available providers, states exact/unknown cost, respects budgets and quotas, and can keep conversation responsive while workers run.

- [x] Class-based Claude/Codex adapters, budgets, usage records and outcome feedback exist; see [BLUEPRINT](BLUEPRINT.md).
- [ ] Prove live selection across at least two usable providers for equivalent bounded tasks, with actual price/usage and failure escalation receipts. API-key executor and automatic cheapest-capable choice remain unaccepted.
- [ ] Prove provider-limit fallback without retry churn or invented cost.
- [ ] Evaluate a small local model on existing hardware for classification/routing only with measured latency, quality and host impact; no local coding model is assumed.

**Estimate:** overlaps Phases 3–5. No honest single completion date until usable provider access and benchmark tasks are fixed. Do not equate a subscription allowance with an API budget.

## Reporting contract

On every meaningful completion or blocker, update the relevant row and the project's actual queue together where authorized, link the implementation SHA, independent gate result, staging/browser proof, live owner acceptance, timestamp, named owner/lane, dependency, next action and revised ETA confidence. Preserve DONE history and label corrections; never turn a draft PR, a worker claim or a stale status comment into LIVE_VERIFIED. The “working now” line must be checked against live worker/process evidence, or say UNKNOWN.

For each active item publish: **requirement · task/PR · owner/lane · stage · started/last activity · implementation SHA · tests · integrated revision · live revision/acceptance · blocker · next action · ETA range and confidence**. If no worker is active, say so only from a fresh host observation. A monitor may report progress, but it does not dispatch work or override a paused lane merely by reading this file.

**Next checkpoint:** isolate the first-Send loss, preserve a reproducible receipt, and gate a narrow fix; then record Michael's microphone timing. Keep web retrieval blocked until its approval packet is decided. No release, spend, seller contact, provider/account change, new assignment, or other project modification is authorized by this checklist.
