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

**Status (2026-10-08).** Design phase. No code exists. The architecture
review is written; eight decisions are waiting on Michael before the first
vertical slice starts.

## Reading order

1. `AGENTS.md` — working rules (inherited from CAOSCare)
2. this file
3. `docs/ARCHITECTURE_REVIEW_2026-10-08.md` — the reviewed architecture:
   what to reuse, what not to build, the minimum viable design, phases, and
   the decisions for Michael (§12)
4. `docs/PROJECT_STATE.md` — dated, append-only build state

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

See review §12. In short: run location during Pilot 1 week; API keys vs
subscription for model usage; integration authority; owner-decision
taxonomy; PR #67 in CAOSCare (persistent agent team) adopt or decline;
`PROJECT_STATE.md` authorship; remote access. Decision 1 (separate repo) is
answered: this repository, created 2026-10-08.

## Next step

Phase 0, the vertical slice (review §11), once the pending decisions that
gate it (run location, payment path) are answered.
