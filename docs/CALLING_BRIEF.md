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
{"command_id": "caller-supplied; else cmd-<sha1 of (conversation_ref + destination_alias + purpose)>[:12] when conversation_ref (the owner turn id) is given; else a fresh cmd-<uuid> — a request with neither is a new command",
 "destination_alias": "fixture:<label>",      // must start with "fixture:"; anything else → rejected
 "purpose": "<= 200 chars, free text",
 "conversation_ref": "the owner turn id (turn-<hex>, minted per Ask Aria owner turn, stored on the owner turn and on the call) or null",
 "source": "aria | panel | test"}
```
Returns the calling-task record:
```json
{"task_id": "calltask-<hex>", "call_id": "call-<hex>", "command_id": "...", "state": "REQUESTED",
 "simulated": true, "adapter": "simulated", "duplicate": false, "created_at": 0.0}
```
A second request with the same `command_id` returns the **same** record with `duplicate: true`, starts
nothing, writes no new receipt (the idempotency table answers). The owner turn id is the durable command
identity for Aria: a model that selects the same `request_call` twice inside one owner turn, or a replay of
that turn, hits the same command; two separate owner turns with identical words are two commands, however
long the transcript is or whether it was trimmed, reset or recovered (the id is minted per turn, not derived
from transcript position). Rejected adapter events (late or out-of-order) are remembered per `(seq, state)` on
the call: the first rejection writes one receipt, an identical replay writes none.

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
9. Correction da-586eaa76af: the same `request_call` action twice in one owner turn → one call, adapter
   started once; two identical owner turns at the transcript cap → two calls; the identical rejected late
   COMPLETED applied twice → one rejection receipt, state still CANCELLED.

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

## 9b. Worker evidence for section 11 (session runner; worker claim, the control plane verifies independently)

- Branch: `agent/implement-docs-calling-brief-md--dfac57` from `main` at `5e7bd48`; the commit SHA is in the worker's final claim.
- Files (implementation line counts): `desktop_agent/control/call_session.py` 282 (new), `desktop_agent/control/calling.py` 190 (was 187), `tests/test_call_session.py` 196 (new, 12 tests); docs: this file, `BLUEPRINT.md` §8 row. `tests/test_calling.py` unchanged.
- Commands and actual results: `bin/run_tests.sh` (full suite) → `140 passed, 2 skipped, 1 warning in 38.67s` (first run, before the doubled-prefix cosmetic fix and the trace print); `.venv/bin/python -m pytest -q -s tests/test_call_session.py -k two_turn` → `1 passed` and printed the trace below. Final full-suite result is in the worker's claim.
- Deviation: tool-call receipts are written on subject `("call_tool", call_id)`, not on `("call", call_id)`: the unchanged `tests/test_calling.py` happy loop requires exactly five receipts on the call chain with the default adapter, which now runs one tool call. Each tool receipt still carries `call_id`, `turn`, `tool_call_id`, `name` in evidence and `call_id`/`command_id`/`calling_task_id` as extras. `CallService.tool_receipt` is the writer; `SessionAdapter` reaches `record`/`tool_receipt` through the bound `apply` (`apply.__self__`).
- Acceptance map (11.5): 1 `test_two_turn_conversation_default_adapter`, `test_default_adapter_sends_tool_result_before_close`; 2 `test_unknown_and_out_of_scope_tools_are_refused`; 3 `test_duplicate_and_replayed_tool_call_ids_execute_once`; 4 `test_interrupted_speech_…`; 5 `test_delayed_and_out_of_order_transcripts`, `test_tool_without_any_transcript_…`; 6 `test_disconnect_without_session_ended_fails`, `test_iterator_end_without_any_event_fails`; 7 `test_cancel_mid_session_ignores_late_events`, `test_cancel_then_events_already_in_flight_do_nothing`; 8 `test_two_overlapping_sessions_do_not_mix`; 9 the unchanged `tests/test_calling.py`.
- End-to-end trace (default adapter, one call; copied from the `-s` run):

```text
verified  call      SIMULATED call command accepted for fixture:front-desk; no real call will be placed
simulated call      SIMULATED call REQUESTED -> ACCEPTED: SIMULATED worker accepted the call
simulated call      SIMULATED call ACCEPTED -> STARTED: SIMULATED call started (no line was opened)
simulated call_tool SIMULATED tool lookup_fixture_status executed for turn 1
simulated call      SIMULATED call STARTED -> IN_PROGRESS: SIMULATED call in progress (fixture conversation)
simulated call      SIMULATED call IN_PROGRESS -> COMPLETED: SIMULATED call completed
turn 1: user: SIMULATED caller: is the fixture office open today?
turn 1: assistant: SIMULATED assistant: let me check the fixture status.
turn 1: lookup_fixture_status: {'ok': True, 'alias': 'fixture:front-desk', 'status': 'SIMULATED: fixture office open', ...}
turn 2: user: SIMULATED caller: thanks, that is all.
turn 2: assistant: SIMULATED assistant: the fixture office is open; goodbye.
```
(The tool receipt is listed between STARTED and IN_PROGRESS here only because the print groups `call_tool` after the first three `call` receipts; the two chains are separate.)

## 10. What remains unimplemented

Code-complete and SIMULATED-only after section 11: the transport interface, `FakeTransport`, the contract-bounded session runner (non-blocking receive loop, per-tool tasks, duplicate/replay safety, disconnect and cancel handling) and `CallService.record`/`tool_receipt`. Still unimplemented: any real transport; real adapter and any provider; destination directory and consent rules; voice transport; a panel card for
calls (the transcript note is the only owner-visible surface in this slice); cost accounting for real
calls; CAOSCare's voice work stays parked and untouched (its `call_lifecycle.advance` compare-and-set,
`TRANSITIONS` table, terminal-outcome receipts and `spoken_call_state` were read as pattern references
only).

## 11. Conversation-control runner behind CallService (owner item da-c929322b10; coordinator spec)

Gap: `SimulatedAdapter` only walks canned states. This slice adds the **session runner**: a
provider-injectable transport interface and an asynchronous event pump that consumes conversation events,
executes contract-bounded local tools, returns their receipted results to the session and finishes the
existing status/result/receipt chain. The fake transport lives on the test/synthetic path only. Nothing here
proves audio or a real call works. Care's `phone_aria_sideband.py` / `phone_aria_tools.py` / `FakeWS` were
read as references (injected socket, transcript events, tool-result round trip, requested-vs-connected truth);
no Care import, data, service or credential is used.

### 11.1 Files

| File | Content | Limit |
|---|---|---|
| `desktop_agent/control/call_session.py` (new) | `SessionTransport` protocol, `FakeTransport`, `CallContract`, `SessionRunner`, `SessionAdapter`, `DEFAULT_FIXTURE_SCRIPT` | ≤ 300 lines |
| `desktop_agent/control/calling.py` | `SimulatedAdapter` becomes a thin constructor over `SessionAdapter` + `FakeTransport` (same signature `delay_sec`, `outcome`, so existing callers and tests keep working); `CallService.record(call_id, entry)` appends a transcript/tool entry to the call record without a receipt; `CallService.apply` unchanged | stays ≤ 300 |
| `tests/test_call_session.py` (new) | acceptance in 11.5 | — |
| `docs/CALLING_BRIEF.md` §9b, §10 | worker evidence; code-complete vs unimplemented | — |
| `docs/BLUEPRINT.md` §8 | update the calling row (session runner, fake transport) | one row |

Untouched: `aria.py`, `api.py`, `service.py`, `calling_api.py`, shared contracts, panel, config.

### 11.2 Transport interface (provider-neutral event shapes)

```python
class SessionTransport(Protocol):
    def events(self) -> AsyncIterator[dict]: ...     # provider → runner; iterator end without session.ended = disconnect
    async def send(self, obj: dict) -> None: ...     # runner → provider
    async def close(self) -> None: ...
```
Inbound: `speech.started {turn}` · `speech.stopped {turn}` · `transcript.user {turn, text}` (may be late or
out of order) · `response.text {turn, text}` · `response.cancelled {turn}` (interrupted speech) ·
`tool.call {turn, tool_call_id, name, args}` · `session.ended {outcome: completed|failed, summary}` (terminal).
Outbound: `tool.result {tool_call_id, output}` · `response.create {}` · `session.close {}`.
`FakeTransport(script, delay_sec=0)` yields the scripted events in order (optionally with a per-event delay or
an explicit `{"type": "disconnect"}` marker that ends the iterator) and records everything sent.

### 11.3 Contract and authority

`CallContract(call_id, allowed_tools)`; the only tool in this slice is `lookup_fixture_status {alias}`: local,
read-only, synthetic (returns a canned dict containing the word SIMULATED). `tool.call` is validated by name
and args; an unknown or out-of-scope name (including `request_call`, `submit_goal`, `send_direction`,
anything not in `allowed_tools`) is **refused** with one `simulated` receipt ("refused: outside the call
contract") and a `tool.result {ok: false}` sent back; refusals never raise and never widen authority. The
runner holds no reference to `CallService.request`, the planner, the inbox or the scheduler: conversation
text cannot start another call or dispatch work. Duplicate `tool_call_id` (including a replayed event) →
the stored output is re-sent, **no second execution and no second receipt**.

### 11.4 Runner

`SessionRunner(call, contract, transport, apply, record)`; `await runner.run()`:
- `apply(STARTED)` once the transport is open; `apply(IN_PROGRESS)` on the first inbound event.
- The **receive loop never blocks on a transcript**: each `tool.call` is handled in its own asyncio task; that
  handler waits (bounded, `TRANSCRIPT_WAIT_SEC = 0.9`) on the per-turn `asyncio.Event` that `transcript.user`
  sets, so a delayed transcript stays consumable while the tool waits for its turn's evidence; the handler
  then executes the tool, writes one `simulated` receipt (evidence: `call_id`, `turn`, `tool_call_id`,
  `name`), records `{"turn", "tool_call_id", "name", "output"}` and sends `tool.result` + `response.create`.
- `transcript.user` / `response.text` → `record({"turn", "role", "text"})`; `response.cancelled` →
  `record({"turn", "event": "response.cancelled"})`; no completion or state change from these.
- `session.ended` → `apply(COMPLETED|FAILED, result={summary, turns, tools, outcome: "SIMULATED: …"})`, then
  `session.close`. Completion follows **only** this terminal event, never elapsed time. Iterator end without
  `session.ended` → `apply(FAILED, "transport disconnected")`.
- `runner.cancel()` (called by `CallService.cancel` through the adapter): sets the cancelled flag, cancels
  pending tool tasks, closes the transport; after it no send, no tool execution, no `apply` happens (the
  existing terminal rule in `CallService.apply` is the second guard). Provenance is per runner instance;
  two runners never share state.

`SessionAdapter(transport_factory)`: `run(call, apply)` builds the transport, contract and runner; keeps the
runner per `call_id` so `cancel` reaches it. `CallService` keeps its default adapter SIMULATED-only:
`SimulatedAdapter(delay_sec=0, outcome="COMPLETED")` = `SessionAdapter` over `FakeTransport(DEFAULT_FIXTURE_SCRIPT)`
(a two-turn fixture conversation with one `lookup_fixture_status` call), so the existing
request → ACCEPTED → STARTED → IN_PROGRESS → COMPLETED chain now runs through the runner.

### 11.5 Acceptance (`tests/test_call_session.py`)

1. Two-turn fixture conversation through `CallService.request` with the default adapter: per-call/per-turn
   correlation in the record, one allowed tool request executed with a receipt (`tool_call_id` in evidence),
   its result sent back to the session, COMPLETED only after `session.ended`, the summary note in the Ask Aria
   transcript (existing `note` path), receipt chain in order.
2. Unknown / out-of-scope tools (`request_call`, `submit_goal`, an unlisted name, bad args) → refused, one
   receipt each, `tool.result {ok: false}` sent, no call created, no task created, state unchanged.
3. Duplicate `tool_call_id` and a replayed `tool.call` event → one execution, one receipt, the same output
   re-sent.
4. Interrupted speech (`response.cancelled`) recorded, conversation continues to completion.
5. Delayed and out-of-order transcripts: `tool.call` arrives before its turn's `transcript.user`; the tool
   result still carries the transcript (within the wait) and a later `transcript.user` for an earlier turn is
   recorded without blocking anything.
6. Transport failure/disconnect (iterator ends without `session.ended`) → FAILED with "transport
   disconnected", no completion note.
7. Cancellation with late events: cancel mid-session → CANCELLED; later `tool.call` / `session.ended` from the
   script produce no send, no execution, no receipt beyond the existing "late result ignored" rule, no false
   completion.
8. Two overlapping sessions (two calls, two transports, interleaved events): each record and receipt chain
   contains only its own `call_id`/`turn`s; no cross-session send.
9. The four command-identity/replay regressions in `tests/test_calling.py` keep passing unchanged.

### 11.6 Still unimplemented after this slice

Any real transport or provider; model selection; outbound telephone origin/bridge; the approved
identity/destination/data/action contract; budget and secure connection setup; audio; panel surface beyond
the transcript note. Care's Asterisk/SIP design is source-only and hardware-unverified and is not assumed to
be a usable Desktop phone connection.
