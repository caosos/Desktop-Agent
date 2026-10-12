# Desktop-Agent onboarding

Boot order:

@AGENTS.md
@START_HERE.md
@docs/PROJECT_STATE.md

For any architecture, adapter, sandbox, event/receipt, or UI work, also
read `docs/ARCHITECTURE_REVIEW_2026-10-08.md` before changing anything.
Do not redesign what the review settled without first naming the existing
decision and why it must change.

Coordinator receipts (ACK / WORKING / BLOCKED / DONE) must carry the ETA-driven check schema in
`AGENTS.md` ("ETA-driven checks and exception alerts"): project, task, responsible, stage, evidence, eta,
eta_basis, next_check_at (America/Chicago), acceptance, blocker. The local watchdog enforces the timer.
