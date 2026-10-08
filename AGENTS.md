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
context. At most two workers at once. Workers never push to `main`, never
merge, never deploy, never hold provider or GitHub secrets. Idle is better
than destructive parallelism.

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

- the change needs a decision that is genuinely Michael's (review §12 and
  the owner-decision taxonomy, once ratified)
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

Before finishing meaningful work, append a dated entry to
`docs/PROJECT_STATE.md` (date, agent/tool, branch, what changed, what was
verified, what is blocked, next safe step). Never erase history; label
corrections. When the platform's own control plane exists, it becomes the
single writer of that file at integration time (review §1.6, §12.7).
