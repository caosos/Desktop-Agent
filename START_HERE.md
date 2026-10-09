# START HERE — Desktop-Agent

**What this is.** A persistent, local-first control plane that supervises
disposable AI workers across Michael's projects, and later acts as a second
set of hands on the Linux desktop. Michael gives a goal in plain language;
the platform reads the project's durable truth, derives bounded tasks,
picks a model, launches one or two fresh sandboxed workers, streams
truthful progress, verifies the result, records receipts with provenance,
integrates safe changes, and interrupts Michael only for genuine owner
decisions or hard external blockers.

**Law.** No action without a receipt. No receipt without provenance.

**Status (2026-10-08).** BUILD, Stage 1 complete on the control-plane side (PR #2, branch `build/stage-1`, ready for review).
Michael authorised BUILD on PR #1. The control plane runs as the user
service `desktop-agent.service` on the EliteDesk (`127.0.0.1:8477`); the
first real CAOSCare task went end to end with a verified receipt chain
(CAOSCARE.COM draft PR #105). Widget code exists; its display check needs
Michael in his desktop session (`docs/WIDGET_SETUP.md`). Current state and
every run, including the failures, are in `docs/PROJECT_STATE.md`.

**Michael-facing surface.** A small desktop widget called Aria (text and
push-to-talk) in Michael's GNOME session, backed by the control plane over a
local API, plus a web instrument panel for inspection. Desktop Aria is a
separate identity from CAOSCare's resident Aria; they share a name only.

## Reading order

1. `AGENTS.md` — working rules (inherited from CAOSCare)
2. this file
3. `docs/OWNER_DIRECTIVES.md` — Michael's direction, dated, in his terms
4. `docs/IMPLEMENTATION_PLAN.md` — system shape, widget design, API,
   dynamic scheduler, stages and acceptance, decision status
5. `docs/ARCHITECTURE_REVIEW_2026-10-08.md` — the reviewed architecture:
   what to reuse, what not to build, sandbox, state/event/receipt model.
   Where it says "max two workers" the directive and plan supersede it.
6. `docs/PROJECT_STATE.md` — dated, append-only build state
7. `docs/BLUEPRINT.md` — the single linked system map: every area, its
   implementing files, and a status label per component
8. `docs/WIDGET_SETUP.md` — running the Aria widget in Michael's session
9. `docs/PANEL_RUNBOOK.md` — what the panel's words and controls mean
10. `docs/BENCHMARK_2026-10-09.md` — audit against OpenHands / CrewAI / LangSmith / Codex / n8n, ranked gaps
11. `docs/IDEA_BOOK.md` — Michael's capture book and the short prioritized
   checklist (max 3), Doing, Done with evidence, Parked decisions

## Running it

- Control plane: `systemctl --user status desktop-agent` (unit file in
  `config/desktop-agent.service`); config in `config/runtime.yaml`; data in
  `~/.local/share/desktop-agent`; workspaces in `~/Desktop-Agent-work/<task>/`.
- Panel: `http://127.0.0.1:8477/` with the token from
  `~/.config/desktop-agent/token`.
- Tests: `.venv/bin/python -m pytest -q` (no Claude, no network).

## Operating the panel (owner)

Full runbook: `docs/PANEL_RUNBOOK.md`. The panel opens in the one-screen **Owner view**; **Details ▸** shows everything.

1. Read the strip at the top: **Done today** (verified, with a PR link and a
   merged / not-merged badge), **Working now**, **Blocked**, and **Your one
   next action**.
2. Decisions are answered in exactly one place, the **Approval packet**: pick
   an answer per item, press **Submit answers**. Blank means unanswered and
   never consent; "defer a day" hides an item without answering.
3. **Submit goal** starts a new bounded worker task (a model runs, cost shows
   on the Cost card). **Transfer / Send** messages a project's existing
   coordinator through GitHub and creates no task.
4. **Coordinators and workers**: COORDINATOR is the project's session;
   WORKERS are jobs running right now from live processes and the project's
   own feed. An idle coordinator or an IDLE project stage does not mean its
   workers are idle.
5. Pause / Resume / Stop all act on this control plane's scheduler only.

## First managed project

`caosos/CAOSCARE.COM`, coordinator checkout `~/CAOSCARE-INTEGRATION` on
`integration/2026-09-27`. Its own project truth (`CAOSCARE_START_HERE.md`,
`AGENTS.md`, `docs/PILOT1_READY_QUEUE.md`, `docs/ENGINEERING_CONTRACT.md`,
`docs/PROJECT_STATE.md`) stays in that repo; this platform points at it and
never copies it.

## Host facts that constrain the design

- EliteDesk `caoscare1-hp-elitedesk`: Ryzen 5 PRO 2400GE (8 threads), 14 GB
  RAM, no discrete GPU. It is also the live CAOSCare Pilot 1 room node.
- Installed: `claude` CLI, `codex` CLI, node 24, python 3.10, git,
  `bwrap`, `systemd-run`. Not installed: tmux, Docker, Podman, Ollama,
  llama.cpp.
- Local models on this host: ≤4B parameters, for routing and labelling
  only. No local coding models (review §6).

## Decisions pending with Michael

Current table: `docs/IMPLEMENTATION_PLAN.md` §7. Answered so far: separate
repository (this one), API keys for model usage, the owner-decision
taxonomy. Open: BUILD authorisation; run location during Pilot 1 week;
integration authority; CAOSCare PR #67; `PROJECT_STATE.md` authorship;
remote access; which repository is "Deal Sniffer".

## Next step

On BUILD: Stage 1 of the implementation plan (widget + control plane + one
CAOSCare task end to end, no terminal), in the build order listed there.
