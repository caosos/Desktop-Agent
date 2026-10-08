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

**Status (2026-10-08).** BUILD, Stage 1 (PR #2, branch `build/stage-1`).
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
7. `docs/WIDGET_SETUP.md` — running the Aria widget in Michael's session

## Running it

- Control plane: `systemctl --user status desktop-agent` (unit file in
  `config/desktop-agent.service`); config in `config/runtime.yaml`; data in
  `~/.local/share/desktop-agent`; workspaces in `~/Desktop-Agent-work/<task>/`.
- Panel: `http://127.0.0.1:8477/` with the token from
  `~/.config/desktop-agent/token`.
- Tests: `.venv/bin/python -m pytest -q` (no Claude, no network).

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
