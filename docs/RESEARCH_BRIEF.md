# Ask Aria research jobs — implementation brief (v1: local sources, one bounded worker per job)

Owner direction: `docs/OWNER_DIRECTIVES.md` 2026-10-09 (late), item 2 ("I don't want to see the assignment.
I want to see what is going to come out of it"; tracked **P3 Research Job Workflow**), row P3 in
`docs/ASK_ARIA_PLAN.md`. Michael, in session 2026-10-11: keep building without waiting.

v1 implements P3 items (1) request parsing, (2) roles, (5) progress / recovery / verified result, with
**local sources only**. Item (3) web retrieval stays **BLOCKED** (the worker sandbox disallows
WebSearch/WebFetch; decision filed in the approval packet 2026-10-10). Item (4) PDF + download is **task B**
(control-plane side, after this task): headless Chrome renders PDF on this host (probe 13 KB, 2026-10-11).

Sections 1–9 are the coordinator's design: the contract the bounded worker implements exactly.
Section 10 is filled in by the worker with its evidence. Section 11 is what stays unimplemented.

## 1. Existing paths reused (nothing new beside them)

| Path | File | How it is reused |
|---|---|---|
| Ask Aria command | `desktop_agent/control/aria.py` (`ACTIONS`, `KIND_BY_ACTION`, `run_action`, `note`) | two new actions `request_research`, `research_status`; progress notes through `note()` |
| Bounded task | `Service.submit_goal(...)` (`control/service.py`, the explicit-contract path `POST /v0/goals` uses) | one research job creates exactly one task; the scheduler, launcher, verifier and integrator are untouched |
| Task outcome | `store.get_task` / `store.list_tasks` (`status` DONE / FAILED / ARCHIVED, `result_json`) | the job follows its task; nothing else is polled |
| Receipts | `receipts.write_receipt` with subject type `research` | one receipt per job transition (see §5) |
| Durable state | `store.set_kv` / `store.get_kv` | `research:<job_id>` records and the `research:index` list |
| Idempotency | `store.idempotent` / `store.remember` | key `research:<turn>`: an Aria turn creates at most one job |
| API | `calling_api.py` router pattern, included from `api.py` with one `include_router` line | `research_api.py` (§6) |
| Tests | `tests/test_calling.py` and `tests/test_aria_brain.py` harness (`_service`, `FakeLLM`, `TestClient`) | `tests/test_research.py` |

Wiring: `Service.__init__` adds `self.research = ResearchService(self.store, self.aria, self)` next to
`self.calling`; `Service.run_forever` calls `self.research.reconcile()` once per scheduler tick (one line,
inside the existing `try`).

## 2. Flow

```
owner text in Ask Aria ──► Aria states the plan (roles, model class, budget) and asks yes/no (same rule as submit_goal)
        │ "yes"
        ▼
Aria action request_research{question, project}
        ▼
ResearchService.request(turn, question, project, source)      dedup on turn → job rj-<hex>  state PLANNED
        │ receipt: "research job planned" (verified, actor control)
        ▼
Service.submit_goal(project, text=<worker brief §3>, task_type="docs",
                    owned_area=["docs/research/<job_id>.md"], expected_artifacts=[same],
                    acceptance_tests=["python3 scripts/research_check.py docs/research/<job_id>.md"],
                    model_class="cloud_strong", budget_usd=1.5, max_attempts=2,
                    why_now="research job <job_id> confirmed by the owner in Ask Aria")
        │ state RUNNING, task_id recorded; aria.note("research job … started: one bounded worker …", kind="research")
        ▼
reconcile() each tick: task DONE → VERIFIED (report path, PR url from result_json if present)
                       task FAILED / ARCHIVED → FAILED (reason from the task result)
        │ receipt + aria.note at each transition
        ▼
same Ask Aria conversation: "research job rj-… VERIFIED: docs/research/rj-….md (citations resolve; content is
the worker's, unverified) — PR …" / "FAILED: …"
```

`model_class` is `cloud_strong` by the planner's own rule ("Sol-class: … research"); the brief says so in the
plan Aria states before asking yes/no. Cost is the task's budget (`budget_usd` 1.5) stated as a ceiling, never a
promise.

## 3. The worker brief (the task `text`, built by `ResearchService.worker_text(job)`)

One worker runs four passes in order and says which pass it is in, in its commit messages:

1. **researcher** — gather evidence from the repository on disk only (docs, code, tests, PROJECT_STATE); no
   web, no guesses; record each fragment as `path:line` plus a ≤120-character quote.
2. **analyst** — group the fragments into findings; a finding without a source is dropped, not padded.
3. **writer** — write `docs/research/<job_id>.md` in the exact format of §4.
4. **reviewer** — check every finding against its cited lines; mark each `OK` or `corrected (…)` in the
   Review section; then run the checker (§4) and fix until it exits 0. The checker is stdlib-only and runs with `python3`: the verifier's clean checkout has no `.venv` (first live job rj-3dba7e19b7 failed on exactly that, 2026-10-11 04:5xZ).

Rules in the text: local repository only; every claim cites `[n]`; the Limits section must say what could not
be answered and that no web retrieval was used; commit on the task branch; do not touch any other file.

## 4. Report format and `scripts/research_check.py`

```
# Research: <question>
job: rj-…  ·  project: <project>  ·  sources: local repository only (no web retrieval)  ·  <YYYY-MM-DD>

## Question
## Findings
1. <one finding> [1][2]
2. …
## Analysis
## Limits
No web retrieval was used. <what could not be answered and why>
## Sources
[1] path/to/file.md:123 — "quoted fragment ≤120 chars"
[2] …
## Review
1. OK — <how it was checked>  |  2. corrected (<what changed>)
```

`scripts/research_check.py <report> [--repo <root>]` exits **0** only when: all seven sections are present in
this order; every numbered Findings item ends with at least one `[n]`; every `[n]` cited anywhere resolves to a
Sources line; every Sources path exists under the repo root and the line number exists in that file; the Limits
section contains the sentence "No web retrieval was used."; the Review section has one entry per finding. It
prints every failed rule (one per line) and exits 1 otherwise. It is the task's acceptance test, so the verifier
runs it in the clean checkout: **the checker verifies structure and that citations resolve, not that the
findings are true** — the receipt and the Aria note say exactly that.

## 5. State, receipts, idempotency

- `TRANSITIONS`: REQUESTED→PLANNED; PLANNED→RUNNING|FAILED|CANCELLED; RUNNING→VERIFIED|FAILED|CANCELLED;
  terminal: VERIFIED, FAILED, CANCELLED. `apply(job_id, state, note)` is compare-and-set under a lock and
  rejects anything else (recorded in the job's `rejected` list, no receipt).
- Record `research:<job_id>`: `job_id, turn, question, project, source, state, task_id, report_path, pr_url,
  reason, history[{ts,state,note}], created_at, updated_at`. `research:index` is the list of ids.
- Receipt per transition: subject `("research", job_id)`, claim `"<STATE>: <note>"`, actor `control`, source
  `research`, label `verified` for control-observed transitions (PLANNED, RUNNING, VERIFIED, CANCELLED),
  `failed` for FAILED; evidence `job_id=…, task_id=…, turn=…`; `correlation_id` = turn.
- `cancel(job_id, source)`: PLANNED/RUNNING → CANCELLED; if a task exists, `await service.control("stop", task_id,
  source)` first; terminal jobs return `{"cancelled": False, "reason": …}`.
- `reconcile()`: for each RUNNING job read its task; DONE → VERIFIED with `report_path` = the owned file and
  `pr_url` from `result_json` when present; FAILED or ARCHIVED → FAILED with the task's reason; otherwise nothing.
  Idempotent: a job is moved once; restarts replay nothing (state is in kv).
- `note()` text always names the job id and the state; a VERIFIED note includes the report path and the sentence
  "citations resolve; the content is the worker's and unverified".

## 6. API (`desktop_agent/control/research_api.py`, included from `api.py`)

`POST /v0/research` `{question, project}` → `request(turn="api-<hex>", …)`; `GET /v0/research` → `{jobs:[…]}`
newest first; `GET /v0/research/{job_id}`; `POST /v0/research/{job_id}/cancel`. All behind the existing `auth`.

## 7. Aria

`ACTIONS["request_research"] = "NEW WORK (research): one bounded worker writes a cited report from local
repository sources only, no web; args {question, project}"`, `ACTIONS["research_status"] = "state, history and
result of one research job: args {job_id}"`, `KIND_BY_ACTION["request_research"] = "research"`. In the system
prompt: a request for a report, investigation or comparison is research; state the plan (four passes in one
worker, model class cloud_strong, budget ceiling $1.5, local sources only) and ask yes/no before
`request_research`; `turn` is passed through exactly as `request_call` does.

## 8. Tests (`tests/test_research.py`)

1. `request` creates one job (PLANNED→RUNNING) and exactly one READY task whose contract has the owned area,
   expected artifact, acceptance test, model class and budget of §2; the Aria note is appended.
2. The same turn twice returns the same job and creates no second task; a different turn creates a new job.
3. `reconcile`: marking the task DONE moves the job to VERIFIED once (second call: no new receipt);
   FAILED → FAILED with the reason; the receipts carry the labels of §5.
4. `cancel` on RUNNING stops the task and records CANCELLED; cancel on VERIFIED is refused.
5. Checker fixtures under `tests/fixtures/research/`: a passing report (citing real repo lines), one with an
   uncited finding, one with a missing path, one without the Limits sentence → exit codes 0/1/1/1 with the
   named rule.
6. Routes: 401 without token; POST creates; GET lists and reads; cancel.
7. Rejected transitions are recorded, not receipted.

## 9. Bounds

`research.py` ≤ 300 lines; `research_api.py` ≈ 50; `scripts/research_check.py` ≤ 150; `aria.py` stays ≤ 300
(split a helper out if needed). No change to the shared contract paths, no network, no new dependency, no
provider or credential. The worker commits on its task branch only; the coordinator reviews and fast-forwards.

## 10. Worker evidence (filled in by the worker)

Self-reported by the worker; the control plane re-runs the gate.

- Files (lines): `control/research.py` 193, `control/research_api.py` 46, `scripts/research_check.py` 84,
  `tests/test_research.py` 178, fixtures `tests/fixtures/research/{pass,uncited,missing_path,no_limits_sentence}.md`;
  `aria.py` 255 (was 248, no helper module needed); one-line edits in `api.py` (router include) and `service.py`
  (`self.research`, `reconcile()` in the tick's `try`).
- Commands: `bin/run_tests.sh` → `tests/test_research.py tests/test_calling.py tests/test_aria_brain.py`: 27 passed;
  full suite `pytest -q`: 168 passed, 2 skipped.
- Deviation (disclosed): `Service.submit_goal` gained `check_duplicate: bool = True`; research jobs pass `False`
  because every job's worker brief (§3) is mostly shared text, so the ≥60 % word-overlap duplicate detector
  rejected a second job with a different question. Turn-level dedup (`research:<turn>`) still applies.
- `BLOCKED` tasks are treated like FAILED/ARCHIVED in `reconcile()` (the scheduler ends exhausted retries as BLOCKED).

## 11. Unimplemented in v1

Web retrieval (BLOCKED decision); PDF rendering and download (task B); one worker per role (v1 runs the four
passes in one worker); research over another project's checkout (v1 targets the project named in the request,
which must be one this control plane manages).
