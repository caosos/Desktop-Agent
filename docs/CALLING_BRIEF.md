# Ask Aria conversational calling — implementation brief (Desktop-only preparation)

Owner instruction `da-24dcdd2b97` (issue #3, 2026-10-10 20:36Z). Scope: one bounded task under the
existing coordinator; the **existing Ask Aria command → one dedicated calling worker → call status and
result back into the same conversation**. Everything that runs here is a **SIMULATED** loop: a
provider-free adapter with fixture identities. No telephony, no provider, no real destination, no
personal data. Code/test completion is separate from owner-visible live acceptance.

Sections 1–8 are the coordinator's integration design (the contract the bounded worker implements).
Section 9 is filled in by the worker with its evidence. Section 10 is what stays unimplemented.

## 1. Existing paths reused (nothing new beside them)

| Path | File | How it is reused |
|---|---|---|
| Ask Aria command | `desktop_agent/control/aria.py` (`ACTIONS`, `run_action`, transcript in kv `aria:history`) | one new action `request_call` and one `call_status`; the call result is appended to the **same transcript** |
| Task / worker ownership | the control plane's own operations with receipts (`receipts.write_receipt`, subject types are free-form) | a calling task is a receipted record owned by the calling worker; it is **not** a row in the code-task table, so the scheduler never launches a sandbox for it |
| Idempotency | `store.idempotent` / `store.remember` (table `idempotency`) | duplicate-command deduplication keyed by `command_id` |
| Durable state | `store.set_kv` / `store.get_kv` | `call:<call_id>` records and the `calls:index` list |
| API | `desktop_agent/control/api.py` (`build_app`, `auth`) | a small router from `calling_api.py` included with two lines |
| Tests | `tests/test_ask_aria.py` harness (`_service`, `FakeLLM`, `TestClient`) | `tests/test_calling.py` builds on the same shapes |

## 2. Flow

```
owner text in Ask Aria ──► Aria step picks action request_call{destination_alias, purpose}
        │                              │
        │                              ▼
        │                CallService.request(command_id, …)   (dedup on command_id)
        │                              │ receipt: command accepted (verified, actor control)
        │                              ▼
        │                calling task  calltask-<hex>  ──owns──►  call  call-<hex>
        │                              │
        │                              ▼
        │                SimulatedAdapter.run(call)  — the dedicated calling worker (in-process asyncio task)
        │                   ACCEPTED → STARTED → IN_PROGRESS (status) → COMPLETED | FAILED
        │                   every transition: compare-and-set + one receipt labelled `simulated`
        │                              │
        ▼                              ▼
same Ask Aria conversation ◄── aria.note("SIMULATED call … started / result …", kind="call (SIMULATED)")
```

Cancel: `CallService.cancel(call_id, source)` from any non-terminal state → `CANCELLED`; a later adapter
result is **rejected** (receipt "late result ignored", state unchanged). Replay: an adapter event with an
already-applied `seq` is ignored, as is a second `request` with the same `command_id`.

## 3. Integration points (exact)

| File | Change | Limit |
|---|---|---|
| `desktop_agent/control/calling.py` (new) | `CallService` (request / cancel / get / list / `apply(call_id, seq, state, detail)`), `TRANSITIONS`, `SimulatedAdapter`, labelling | ≤ 300 lines |
| `desktop_agent/control/calling_api.py` (new) | `calling_routes(service, auth) -> APIRouter`: `POST /v0/calls`, `GET /v0/calls`, `GET /v0/calls/{call_id}`, `POST /v0/calls/{call_id}/cancel` | small |
| `desktop_agent/control/api.py` | import + `app.include_router(calling_routes(service, auth))` | 2 lines |
| `desktop_agent/control/service.py` | `self.calling = CallService(self.store, self.aria)` after `self.aria` is built | ≤ 5 lines |
| `desktop_agent/control/aria.py` | actions `request_call`, `call_status`; `KIND_BY_ACTION["request_call"] = "call (SIMULATED)"`; `note(text, kind, evidence)` that appends one `aria` turn to the transcript and persists; system-prompt sentence: calls are SIMULATED, no real call is ever placed, destinations are fixture labels | small |
| `tests/test_calling.py` (new) | the loop and its regressions (section 7) | — |
| `docs/BLUEPRINT.md` §8 | one row: conversational calling, SIMULATED adapter only, cites `tests/test_calling.py`; real provider PROPOSED | one row |
| `docs/ASK_ARIA_PLAN.md` | one row: conversational calling **PARTIAL** (synthetic loop; real calls BLOCKED on the owner gate in section 8) | one row |

Shared contracts (`events.py`, `receipts.py`, `contracts.py`) are **not** touched. Calls do not use the task
events table; the receipt chain on subject `("call", call_id)` is the trace.

## 4. Ownership

- **Coordinator (this control plane, `caoscare-1-05`)**: integration, canonical ledger (receipts + kv), the
  Ask Aria entry point, and the only place a future real adapter could be enabled.
- **Calling worker**: one calling task owns exactly one call; it drives the adapter, applies transitions
  through `CallService.apply` (never by writing state directly), reports status and the final result, then
  exits. Today it is the in-process `SimulatedAdapter`; a future real worker keeps this interface.
- **Adapter boundary**: `CallAdapter.run(call, apply)`. The simulated adapter has no network, no provider,
  no credentials; it must be the only adapter the code can construct.

## 5. Request contract

```json
{"command_id": "cmd-<sha1 of (conversation_ref + destination_alias + purpose)>[:12] or caller-supplied",
 "destination_alias": "fixture:<label>",      // must start with "fixture:"; anything else → rejected
 "purpose": "<= 200 chars, free text",
 "conversation_ref": "aria turn index or null",
 "source": "aria | panel | test"}
```
Returns the calling-task record:
```json
{"task_id": "calltask-<hex>", "call_id": "call-<hex>", "command_id": "...", "state": "REQUESTED",
 "simulated": true, "adapter": "simulated", "duplicate": false, "created_at": 0.0}
```
A second request with the same `command_id` returns the **same** record with `duplicate: true`, starts
nothing, writes no new receipt (the idempotency table answers).

## 6. Result contract (`GET /v0/calls/{call_id}` and the transcript note)

```json
{"call_id": "...", "task_id": "...", "command_id": "...", "conversation_ref": "...",
 "destination_alias": "fixture:front-desk", "purpose": "...",
 "state": "REQUESTED|ACCEPTED|STARTED|IN_PROGRESS|COMPLETED|FAILED|CANCELLED",
 "simulated": true, "label": "SIMULATED",
 "history": [{"seq": 1, "state": "ACCEPTED", "at": 0.0, "source": "simulated", "detail": "..."}],
 "result": {"outcome": "SIMULATED: completed", "summary": "...", "duration_sec": 0.0} | null,
 "receipts": 5}
```
Every adapter-produced state, result, summary and transcript note carries the literal word **SIMULATED**.
Receipt labels: `verified` for the control plane's own acceptance/cancel of a command; `simulated` for every
adapter transition and result. A worker-style actor never produces `verified`.

## 7. Rules the tests must prove (`tests/test_calling.py`)

1. Happy loop with zero adapter delay: request → ACCEPTED → STARTED → IN_PROGRESS → COMPLETED; the
   receipt chain on `("call", call_id)` is in order and linked; `command_id`, `task_id`, `call_id` are
   present on every receipt (evidence or extra); the Aria transcript gains the start note and the
   SIMULATED result note, both referencing `call_id`.
2. Duplicate command: same `command_id` twice → same task/call, `duplicate: true`, exactly one call
   record, receipt count unchanged, adapter started once.
3. Replay: applying an adapter event with an already-applied `seq` → `applied: false`, no receipt, state
   unchanged.
4. Cancel vs late result: cancel while STARTED → CANCELLED (receipt); a later COMPLETED from the adapter
   → `applied: false`, one receipt "late result ignored", state still CANCELLED, no result note in the
   transcript, no second call.
5. Fixture identities only: `destination_alias` without the `fixture:` prefix → `ValueError` /
   HTTP 400; no record created.
6. Ask Aria path with `FakeLLM`: an owner message → `request_call` action → reply kind
   `call (SIMULATED)`, action evidence carries `call_id`; after the adapter finishes, the transcript's last
   turn contains "SIMULATED".
7. API: `POST /v0/calls` (201), `GET /v0/calls/{id}`, `POST …/cancel`, with the bearer token.
8. Out-of-order adapter events (e.g. COMPLETED before STARTED) are rejected by `TRANSITIONS`.

## 8. Authorization boundary for future real calls (not built)

A real call would require **all** of: an owner decision in the approval packet naming the provider and the
destination directory; a config gate `providers.telephony.enabled: false` by default (same pattern as the
OpenAI gate, flipped only by the owner's own edit); destinations only from an owner-maintained directory of
labels (Aria never sees or speaks a raw number); a per-call owner confirmation step before dialing; cost
recorded per call or UNKNOWN, never estimated; and the adapter constructed only when that gate is on.
Until then the code can construct the simulated adapter only.

## 9. Worker evidence (filled by the bounded worker; the control plane verifies independently)

- Branch / commit: `agent/implement-docs-calling-brief-md--52fe05` from `main` at `398710e`; commit SHA is in the worker's final claim (a commit cannot name itself).
- Files changed with line counts (implementation files): `desktop_agent/control/calling.py` 177 (new), `calling_api.py` 45 (new), `aria.py` 226, `api.py` 344 (+2 lines), `service.py` 578 (+3 lines), `tests/test_calling.py` 146 (new); docs: this file, `BLUEPRINT.md` §8, `ASK_ARIA_PLAN.md` (one row each). `api.py` and `service.py` were already over 300 lines before this change.
- Commands run and actual results: `bin/run_tests.sh` (full suite, `.venv/bin/python -m pytest -q`): first run 1 failed (the API test's zero-delay adapter finished before the cancel arrived; fixed by a 30 s adapter in that test), final run `124 passed, 2 skipped, 1 warning in 33.94s`. `bin/run_tests.sh tests/test_calling.py -s -k happy` printed the trace below. Worker claim only; the control plane re-runs the gate.
- One trace (command → worker START → status → SIMULATED result / receipt), copied from a test run (`test_happy_loop_receipts_and_transcript`):

```text
verified  control        SIMULATED call command accepted for fixture:front-desk; no real call will be placed
simulated calling_worker SIMULATED call REQUESTED -> ACCEPTED: SIMULATED worker accepted the call
simulated calling_worker SIMULATED call ACCEPTED -> STARTED: SIMULATED call started (no line was opened)
simulated calling_worker SIMULATED call STARTED -> IN_PROGRESS: SIMULATED call in progress (fixture conversation)
simulated calling_worker SIMULATED call IN_PROGRESS -> COMPLETED: SIMULATED call completed
transcript: SIMULATED call call-6d6ba7e336 to fixture:front-desk started (confirm the fixture appointment).
transcript: SIMULATED call call-6d6ba7e336 completed: SIMULATED call to fixture:front-desk about 'confirm the fixture appointment' completed with a fixture answer; no real call was placed.
```

Deviations: the cancel path also appends a SIMULATED transcript note ("was cancelled"); an out-of-order event writes a `simulated` receipt ("out-of-order event rejected") without advancing `seq`; the calling task is not a row in the code-task table, so the scheduler never sees it.

## 10. What remains unimplemented

Real adapter and any provider; destination directory and consent rules; voice transport; a panel card for
calls (the transcript note is the only owner-visible surface in this slice); cost accounting for real
calls; CAOSCare's voice work stays parked and untouched (its `call_lifecycle.advance` compare-and-set,
`TRANSITIONS` table, terminal-outcome receipts and `spoken_call_state` were read as pattern references
only).
