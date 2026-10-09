# Idea Book

Michael's capture book and the short checklist that comes out of it. One document,
owner-visible, maintained by the coordinator from Michael's notes, Aria conversations and
owner comments. Projects keep their own queues (CAOSCare `docs/PILOT1_READY_QUEUE.md`,
Michael Business OS `docs/status/READY_QUEUE.md`); this book never dispatches work into them.

**Rule (Michael):** "Capture everything. Execute one thing. Finish it. Then move."
Capturing an idea is not authorization to build it. A checklist item is checked only on evidence
(a receipt, a verified task, a merged change, a thing Michael saw work).

---

## 1. Prioritized checklist — owner-approved NEXT actions only (max 3)

| # | Action | Project | Depends on | Done when (verifiable) |
|---|---|---|---|---|
| **1** | **Optional: the first OpenAI paid pilot** (`d-e0c933ff`, in the approval packet) — stays DISABLED unless you answer yes **and** a hard spend limit is set in the OpenAI dashboard (the platform's $10 cap is software only). Unanswered means nothing changes | Desktop-Agent | key installed ✓ | decision recorded; `providers.openai.enabled: true` committed; first call's tokens appear on the panel with a known or UNKNOWN cost, matching the Console's usage page |
| 2 | Ask Aria MVP: text chat + dictation in the panel (P1, in progress) — your only input needed later: allow the browser microphone when Chrome asks, and say whether an on-device speech model may be installed | Desktop-Agent | P0 ✓ | you dictate a question, edit it, press Send, and get an evidence-backed answer on :8477 |
| 4 | **Optional** widget, one action: in your own desktop login run `bash <(curl -fsSL https://raw.githubusercontent.com/caosos/Desktop-Agent/main/widget/install.sh)` once, then press **Pair widget** on :8477 and type the code into the widget. The panel at :8477 is already the complete owner interface; skip this if you do not want a desktop window | Desktop-Agent | nothing (`docs/WIDGET_SETUP.md`) | a `widget paired` event on :8477 and Aria answers one question from the widget |
| 3 | Optional: the Anthropic API key decision (`d-528e97b0`, in the approval packet). Not required: subscription workers keep running; API usage is metered separately and no included credits are assumed | Desktop-Agent | nothing | decision recorded; if yes, `anthropic.key` present and `--check-providers` shows it |

Sorted by utility and finishability: #1 is one answer that unlocks metered costs; #2 is the Stage 1 acceptance observation; #3 is a yes/no.

## 2. Doing (limit 2)

| What | Who | Since | Evidence so far |
|---|---|---|---|
| Michael Business OS liaison round trip: message `ARIA-20261008-2209-desktop-agent-central-monitor-now-reads` awaiting Agent 01's ack file | watchdog (free poll) | 2026-10-09 03:10Z | item `da-9284d4f259` SENT; 11/12 liaison messages ACKNOWLEDGED |
| PR #11 (widget-is-optional wording in `docs/WIDGET_SETUP.md` and README, written by a Luna-class worker, verified) waits for a merge click: the coordinator's merge was refused by the Claude Code auto-mode classifier ("Merge Without Review") | Michael | 2026-10-09 18:10Z | https://github.com/caosos/Desktop-Agent/pull/11 |

## 3. Done (plain language, with evidence)

| Date | Result | Evidence |
|---|---|---|
| 2026-10-09 | **Clickable project drilldowns** on the Owner view: coordinator vs workers, agent roster quoted from each project's own table with actual-now from live evidence only (Business OS: 7 lanes, all NOT RUNNING at the time), tasks with evidence, instructions with age and honest next action, freshness, one DO line | `da-fad659ca3c` P0; `tests/test_drilldown.py`, `tests/test_owner_states.py`; `docs/ASK_ARIA_PLAN.md` |
| 2026-10-09 | **Benchmark vs OpenHands / CrewAI AMP / LangSmith / Codex / n8n** (`docs/BENCHMARK_2026-10-09.md`): most patterns already here with receipts; built the evidence trace per task (stage timeline, cost, every event with provenance) reachable from the Owner view; owner-click merge documented as a build-ready proposal after the tool classifier refused it; seeded live-like states test at three widths; Done badge bug (open PR shown as merged) fixed | owner order `da-623f447d9b`; `tests/test_owner_states.py` |
| 2026-10-09 | **Owner view by default on :8477**: three concise project cards with amber alerts from real telemetry (live: Business OS dispatcher stopped with 1 row queued), top 3 outcomes, decisions with the packet on demand, one NEXT; Details ▸ keeps everything else; runbook `docs/PANEL_RUNBOOK.md` | owner priority `da-92e77fd0ba`; `tests/test_panel_browser.py`, live acceptance; `docs/PROJECT_STATE.md` 2026-10-09 |
| 2026-10-09 | **WORKERS shown separately from COORDINATOR on the Coordinators card**, from grounded sources only (own task records, live host processes per declared pattern, MBOS feed on :8479, open-PR check): RUNNING / STALE / FINISHED / WAITING / IDLE / UNKNOWN ("worker runtime not verified"). Live: CAOSCare coordinator idle while 2 workers run (rq-050, rq-051); CAOSCARE.COM PR #110 and Desktop-Agent PR #11 finished, not merged; MBOS idle, dispatcher running | owner observation `da-a1d0c6c065`; `control/workers.py`, `tests/test_workers.py`; `docs/PROJECT_STATE.md` 2026-10-09 |
| 2026-10-09 | **First-time-owner acceptance passed on the live :8477 with real data from all three projects** (8 done with evidence links, blocked list empty, 2 open decisions in one packet, 11 read-only external gates, Shared Inbox statuses equal to the records, idle CAOSCare coordinator still shown by session name, reload keeps the picture, 400 px wide without sideways scroll). One defect found and fixed: long urls and chips widened the page on a phone. A failed delivery is shown as RECEIVED + "delivery failed" + UNACKNOWLEDGED, never DELIVERED | `tests/test_live_acceptance.py` (`DA_LIVE=1`), `tests/test_panel_browser.py`; screenshots in `~/.local/share/desktop-agent/live-acceptance-*.png`; commit on `main` 2026-10-09 |
| 2026-10-09 | **OpenAI key installed by Michael** at `~/.config/desktop-agent/openai.key` (mode 0600); verified from the control-plane account with the free provider check: key present, 133 model ids; no paid call; provider stays disabled until the pilot is approved | `--check-providers` run 2026-10-09; decision `d-58f742d0` answered "done" with that evidence; `config/runtime.yaml` |
| 2026-10-09 | Owner instructions on GitHub now reach the running coordinators automatically and their acknowledgments show on :8477; CAOSCare and Desktop-Agent paths verified end to end, Business OS monitored via its liaison branch | `docs/PROJECT_STATE.md` 2026-10-09 entries; issue #3 and CAOSCARE.COM #117 comments |
| 2026-10-09 | Coordinator watchdog: cheap checks, one bounded wake only on a WAKE probe, quota windows shown, auto-resume after a reset | same; 68 tests |
| 2026-10-09 | System blueprint written by the platform itself and merged | `docs/BLUEPRINT.md` (PR #10) |
| 2026-10-09 | Provider setup report with official sources; Anthropic prices configured; no paid calls | `docs/PROVIDER_SETUP_REPORT.md` |
| 2026-10-08 | Stage 2 core: planner, concurrency with feedback, owner inbox, server-side Aria, Shared Inbox | PRs #6–#9; state entries |
| 2026-10-08 | Stage 1: control plane, sandboxed workers, first real CAOSCare tasks verified (CAOSCARE.COM PRs #105, #110) | PRs #2, #4, #5 |

Counts: 8 platform tasks verified and merged on 2026-10-08/09; ~$8 known spend plus unpriced subscription use.

## 4. Parked decisions (return to them without losing the thought)

| Captured | Thought | Why parked | Return when |
|---|---|---|---|
| 2026-10-08 | Egress allowlist / network namespace for workers | cuts workers off from the host's Mongo and providers without a proxy; threat model does not justify it yet | a project or host needs it |
| 2026-10-08 | Desktop control tools (files, apps, browser) through governed MCP tools | staged for Phase 3; separate permissions and acceptance | after the widget is in daily use |
| 2026-10-08 | Local models (Pi 5 / EliteDesk ≤4B) for labelling and routing | no coding value on this hardware; room node must stay unloaded during Pilot 1 | hardware upgrade or an idle second box |
| 2026-10-08 | Raspberry Pi as the always-on control-plane host | useful, not urgent; EliteDesk carries it today | if build load disturbs the room node |
| 2026-10-08 | Tailscale for phone access to :8477 | owner decision (review §12.8); LAN works | when Michael wants the panel off-site |
| 2026-10-09 | Deal Sniffer Operator UI at localhost:8765 shown inside :8477 | owner said "later", after Desktop-Agent's own interface is usable | after checklist #2 |
| 2026-10-09 | Owner-click merge of verified Desktop-Agent PRs from the panel (benchmark §4) | the coordinator's tool classifier refused building the merge primitive | Michael allows it in his Claude Code permission rules and assigns the build |
| 2026-10-09 | Owner-defined scheduled automations (benchmark #3) | not started; medium value | after the two open owner actions clear |
| 2026-10-09 | Event-driven wake for Michael Business OS Agent 01 | its coordinator runs under another Linux account; no channel from the control plane | if Michael moves it to the control-plane account or adds a cross-account channel |

## 5. Idea Book — capture (newest first; one line each; source and project)

| Date | Idea / note | Source | Project |
|---|---|---|---|
| 2026-10-09 | "When I make notes they don't help me a lot until I have some kind of checklist made out of them in priority" → this document | owner, issue #3 (`da-4624e90014`) | all |
| 2026-10-09 | One dashboard, three projects, two-way communication, clear approvals, live verifiable work | owner, issue #3 (`da-e2ab69583a`) | Desktop-Agent |
| 2026-10-09 | Aria greets and walks through pending decisions one at a time | owner, issue #3 (`da-56edcaa991`) | Desktop-Agent |
| 2026-10-08 | Model classes named Luna / Sol / Astra; least expensive capable, escalate on evidence | owner directive | all |
| 2026-10-08 | Widget: small, cool, clean, color-coded; a thing to talk to | owner directive | Desktop-Agent |
| 2026-10-08 | Possible RAM to 32 GB and storage to 500 GB–1 TB | owner directive | host |
| 2026-10-08 | eMeet microphone placement vs the EliteDesk fan as a wake-detection confound | owner test notes, CAOSCARE.COM #117 | CAOSCare |

### Sample: how one note became a checklist item
Note (2026-10-08, owner): "Go ahead and figure out this whole OpenAI and Anthropic API key." →
captured above → turned into checklist **#1** only after the preparatory work that needs no owner
was finished (provider layer built, verified with zero paid calls, report written) and the single
remaining step was identified as an owner action with a verifiable "done when" → it will be checked
off when `--check-providers` shows the key present, not when the command is believed to have run.

---

*Maintenance:* the coordinator appends captures as they arrive (intake items, Aria conversations,
owner notes), promotes at most three to the checklist with Michael's approval, moves finished items
to Done with their evidence, and parks the rest with a return condition. Edits are commits with
receipts like everything else in this repository.
