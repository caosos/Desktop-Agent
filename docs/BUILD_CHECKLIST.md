# Desktop Agent — Living Product Build Checklist

**Snapshot:** 2026-10-10, approximately 22:00 CDT (GitHub evidence through 2026-10-11 02:51 UTC). **Scope:** `caosos/Desktop-Agent` only. This is a planning and reporting record, not a worker dispatch, deployment, provider change, or release approval.

**Product finish line:** Michael talks or types to Aria on his desktop; Aria can manage the three projects, use approved desktop/browser/SSH tools, derive and run useful bounded work with economical model choice, show live progress and receipts, and keep going while Michael is away. The owner-facing screen must remain understandable. The full finish line has **not** passed acceptance.

**Evidence rule:** no action without a receipt; no receipt without provenance. A commit proves code exists, a test proves its tested behavior, a host observation proves what ran there, and Michael's use proves the owner experience. Do not check a whole feature DONE from a worker claim, a passing unit test, or a draft PR. The canonical event log is [PROJECT_STATE](PROJECT_STATE.md); [ASK_ARIA_PLAN](ASK_ARIA_PLAN.md), [BLUEPRINT](BLUEPRINT.md), [RESEARCH_BRIEF](RESEARCH_BRIEF.md), and [Issue #3](https://github.com/caosos/Desktop-Agent/issues/3) carry the detailed contracts and receipts. If this snapshot conflicts with a newer receipt, update it and link the correction.

## At a glance

- **Current milestone:** Phase 2/6, make Ask Aria reliably usable from Michael's screen. Phases 3, 4, and 6 have work in parallel; phase numbers are acceptance milestones, not a serial ban on useful work.
- **Current code activity observed on GitHub:** [PR #14](https://github.com/caosos/Desktop-Agent/pull/14), local-source research jobs v1, is an open draft at `3374d095` (13 files, verifier run described in its PR). Integration and live use are not evidenced in this snapshot. [PDF renderer branch `feature/research-pdf`](https://github.com/caosos/Desktop-Agent/commit/df1422f7f72eb462f2da32be620881221a7b236d) has a reported three-test prototype, with no integrated/host acceptance receipt found. The coordinator's [latest Issue #3 activity note](https://github.com/caosos/Desktop-Agent/issues/3#issuecomment-6104756919) called these in progress.
- **Separate local Codex candidate:** `fix/aria-dictation-response` at `1abee06aa2eaf141a2ad5e3da49a3c2881e70dcf` addresses the `claude_cli` one-turn failure and interim dictation display. It is **LOCAL ONLY**, not in GitHub, not deployed, and not microphone-accepted. Do not duplicate it without comparing with current `main`.
- **Last recorded live Ask Aria acceptance:** `a42f4e3` answered “what is blocked” in Chrome after compact-state repair, using one state read; [PROJECT_STATE](PROJECT_STATE.md) entry “Ask Aria answers what is blocked”. That does not prove Michael's later multi-step question or speech path works.
- **Live process and worker status now:** **UNKNOWN from GitHub**. The last host receipt reported the service running after `a42f4e3`; a GitHub PR or silence cannot establish a current process. Do not display “working now” from an old comment.
- **Next engineering checkpoint:** independently review PR #14 against its brief, integrate only after the gate and receipt, then prove a real research request and report readback. In parallel, reconcile the PDF branch and the local Ask Aria/dictation candidate without racing the coordinator's files.
- **Needs Michael now:** no decision identified for the research code review. A real microphone/dictation acceptance and optional GTK widget acceptance require Michael at his desktop. Web retrieval and real calling remain separate owner gates.

## Reconciliation 2026-10-11 06:1xZ (host evidence; the snapshot above is kept as written)

- [PR #14](https://github.com/caosos/Desktop-Agent/pull/14) research jobs v1, [PR #16](https://github.com/caosos/Desktop-Agent/pull/16) (`claude_cli --max-turns 3`, interim dictation) and [PR #15](https://github.com/caosos/Desktop-Agent/pull/15) are **merged**; the PDF publish path is on `main` (`347c162`); the first live research job `rj-fd023afe99` is VERIFIED end to end with its PDF served ([PROJECT_STATE](PROJECT_STATE.md) 04:4x–05:5xZ). Michael's exact multi-step question answered live at 04:36Z.
- Lost first Send: cause proven and fixed in [`de3b979`](https://github.com/caosos/Desktop-Agent/commit/de3b979406a57134a07b2f02291d6800573c4f87) (focus before fetch; owner turn persisted on arrival), loaded 06:03:50Z, verified in real Chrome on the host. Michael's own-browser re-check and the microphone timing remain his.
- **Loaded revision now:** `de3b979` (MainPID 313008). **Next item:** DA-REBOOT-CONTINUITY-001 (`da-588ba1951a`), then the Phase 2 microphone/owner-UX gap when Michael is at his desk.

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
| Ask Aria text, project-grounded status, clear blocked/owner answer | ✓ | ✓ | ✓ | ✓ | [`a42f4e3`](https://github.com/caosos/Desktop-Agent/commit/a42f4e399561bde394643af8014714705f2693df) passed gate and Chrome question; retain source names and current freshness in replies. |
| Multi-step Ask Aria question avoids `claude_cli` maximum-turns failure | ✓ | ✓ | ✓ | ✓ | Owner's observed failure: “Reached maximum number of turns (1).” Local `1abee06` changes this path; focused tests reported passing, browser/EliteDesk retest open. Publish/review without duplicating another worker's change. |
| Dictation shows interim words promptly and final words remain editable | ✓ | ✓ | ✓ | · | Current panel waits for final recognition segments; local `1abee06` adds interim display. No Chrome microphone test here; measure capture → interim → final → Send on Michael's machine. |
| First Send after a page load always reaches the control plane; the request is in the store on arrival | ✓ | ✓ | ✓ | ✓ | [`de3b979`](https://github.com/caosos/Desktop-Agent/commit/de3b979406a57134a07b2f02291d6800573c4f87): focus race proven with real keyboard input, regressions fail-before/pass-after, live probes on the loaded service (PROJECT_STATE 2026-10-11 05:4x–06:1xZ). Michael's browser over the tunnel not re-driven yet. |
| Panel is understandable while work runs; statuses and receipts are truthful | ✓ | ✓ | ✓ | ✓ | [`adee737`](https://github.com/caosos/Desktop-Agent/commit/adee73730536f76c6c4986ddbf0bb494a240d219) corrected blocked labels and was checked in Chrome. Michael reports the panel still feels busy; get a task-driven owner walk-through before closing usability. |
| Optional GTK widget and push-to-talk path from Michael's desktop | ✓ | ✓ | · | · | [Widget setup](WIDGET_SETUP.md) and unit tests exist; an actual display/voice acceptance receipt remains open. The browser panel is the usable owner surface meanwhile. |

**Next milestone order:** review and integrate the one-turn/dictation fix; test Michael's exact failing question; measure speech latency with a real microphone; issue one bounded task from the owner surface and inspect its receipt. **Estimate:** 1–3 focused working days plus Michael's short desktop check, **low confidence** until host tests.

## Phase 3 — Reliable autonomous build supervisor across three projects

**Milestone:** Aria derives the next eligible bounded task, supervises useful parallel work within budget, prevents duplicate/conflicting dispatch, recovers from interruption, handles approval/denial paths, and reports only actual owner decisions for a full supervised day.

| Check | I | T | S | L | Evidence / open gate |
|---|:---:|:---:|:---:|:---:|---|
| Planner, scheduler, two concurrent workers, Claude and Codex paths, receipt-backed verification | ✓ | ✓ | ✓ | ✓ | [BLUEPRINT](BLUEPRINT.md) and [PROJECT_STATE](PROJECT_STATE.md) record live runs; concurrency remains resource/budget/dependency-driven. |
| Three-project cards, project drilldowns, shared inbox and approval packet | ✓ | ✓ | ✓ | ✓ | [ASK_ARIA_PLAN](ASK_ARIA_PLAN.md), [PROJECT_STATE](PROJECT_STATE.md); Business OS liaison is manual-only in the latest host record, so full cross-account control is open. |
| Automatic next useful work, deduplication, cancellation/restart recovery, genuine owner escalation | ✓ | ✓ | · | · | Individual mechanisms exist, but the full-day unattended acceptance and queue derivation when READY is empty are not proven. Compare the LOCAL ONLY hardening before any port. |
| Current liveness and actionable notifications, not stale GitHub inference | ✓ | ✓ | · | · | Intake and panel exist; verify worker completion → coordinator acknowledgment → checklist/queue update → one owner alert on a real run. |

**Estimate to acceptance:** 5–10 focused working days after Phase 2's dependable command loop; **low confidence**, and longer if cross-account wake needs new permissions.

## Phase 4 — Research and useful artifacts through Ask Aria (ACTIVE parallel work)

**Milestone:** Michael requests a real report, Aria states source and budget limits, one bounded worker researches, verifies citations, makes a usable PDF, and returns a link, progress and receipts in the conversation.

| Check | I | T | S | L | Evidence / open gate |
|---|:---:|:---:|:---:|:---:|---|
| Local-source research job, job state, deduplication, cancellation, report checker | ✓ | ✓ | · | · | Draft [PR #14](https://github.com/caosos/Desktop-Agent/pull/14) at `3374d095` reports control-plane verification; independent review/integration and an owner request/readback remain open. Checker proves citation shape and referenced lines, not the truth of conclusions. |
| PDF render and download from completed report | ✓ | ✓ | · | · | [`df1422f`](https://github.com/caosos/Desktop-Agent/commit/df1422f7f72eb462f2da32be620881221a7b236d) is a branch prototype; integrate and accept with an actual report. |
| Web research with citations | · | · | · | · | Web retrieval is blocked in the worker sandbox; the approval-packet decision and source policy are recorded in [RESEARCH_BRIEF](RESEARCH_BRIEF.md). Local-source work need not wait. |

**Immediate estimates:** PR #14 review/integration and real local report, 0.5–1.5 focused days; PDF integration and readback, 0.5–1.5 more days; **low confidence** until review finds the remaining defects. Web retrieval has no ETA pending its decision.

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

**Next checkpoint:** reconcile PR #14 and the PDF branch with the live coordinator, then revisit the local one-turn/dictation fix. Update this file from the results. No release, spend, seller contact, provider/account change or other project modification is authorized by this checklist.
