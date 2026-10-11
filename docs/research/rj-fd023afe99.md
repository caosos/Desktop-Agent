# Research: What does the receipt law require, and where in the code is it enforced?
job: rj-fd023afe99  ·  project: desktop_agent  ·  sources: local repository only (no web retrieval)  ·  2026-10-10

## Question
What does the receipt law require, and where in the code is it enforced? Produce a cited report.

## Findings
1. The law is stated as "No action without a receipt. No receipt without provenance." and applies to every meaningful action or state change by a human, agent, scheduler, provider, adapter or integration. [1][2]
2. A receipt must link to its origin: who initiated it, why it was authorised, what changed, what executed it and what evidence proves it. A worker's self-report is never proof. [2][3]
3. Result labels are `verified` (observed by the control plane or Michael), `unverified` (self-report), `simulated` or `failed`; the code defines exactly these four and rejects any other label. [4][5][6]
4. Code enforces that a worker cannot produce a `verified` receipt: `write_receipt` raises when the actor is the worker and the label is `verified`. [7]
5. Code enforces "no receipt without provenance": `write_receipt` raises when evidence is empty, and when actor or source is missing. [8][9]
6. Receipts form a chain: each new receipt records the previous receipt of the same subject as `parent_receipt_id`, and `chain` returns them in order. [10][11]
7. Receipts are persisted durably: `save_receipt` inserts into SQLite and also appends to a per-task `receipts.jsonl`. [12][13]
8. The worker's claim is recorded by the launcher as an `unverified` receipt with worker, head and session evidence. [14]
9. The verifier, run by the control plane, writes the `verified` or `failed` receipt from its own observation. [15][16]
10. The architecture review specifies the same rules (worker only `unverified`; `verified` needs the verifier's observation; every receipt has actor and evidence) and the receipt shape. [17][18]
11. A test exercises the law: worker + `verified` raises, empty evidence raises, and a worker `unverified` receipt is followed by a control `verified` one. [19][20]

## Analysis
The law is a policy statement in AGENTS.md [1][2] restated as a design invariant in the architecture review [17][18], and the core of it is enforced in a single function, `write_receipt` [7][8][9]. Two rules are hard-coded there: no worker-authored `verified`, and no receipt lacking evidence, actor or source. The "no action without a receipt" half is not enforced by that function (it can only validate receipts that are written); it is carried by callers, which write receipts at state changes such as the worker claim [14] and the verifier outcome [15]. The separation of claim (`unverified`, worker) from verification (`verified`/`failed`, control plane) is realised by the launcher and verifier writing the two receipts [14][15][16]. Chaining via `parent_receipt_id` [10] and dual persistence [12][13] provide the durable trail.

## Limits
No web retrieval was used. Only the repository checkout was read. This report does not enumerate every `write_receipt` call site, so it cannot confirm that every state change in the codebase has a receipt; the "no action without a receipt" half is therefore not verified here beyond the two sites cited. The label check does not validate what the evidence items contain, only that the list is non-empty [8]; whether evidence is truthful is not enforced in code. The `actor` check compares only to the worker actor value [7]; other actors (e.g. adapters) are not restricted from `verified` by this function.

## Sources
[1] AGENTS.md:34 — "**No action without a receipt. No receipt without provenance.**"
[2] AGENTS.md:36 — "Every meaningful action or state change by a human, agent, scheduler,"
[3] AGENTS.md:40 — "self-report ("done", "tests passed") is never proof. Coding work needs"
[4] AGENTS.md:43 — "`verified` (observed by the control plane or Michael), `unverified`"
[5] desktop_agent/control/receipts.py:20 — "_LABELS = {VERIFIED, UNVERIFIED, SIMULATED, FAILED}"
[6] desktop_agent/control/receipts.py:33 — "if result_label not in _LABELS:"
[7] desktop_agent/control/receipts.py:35 — "if actor == Actor.WORKER.value and result_label == VERIFIED:"
[8] desktop_agent/control/receipts.py:37 — "if not evidence:"
[9] desktop_agent/control/receipts.py:39 — "if not actor or not source:"
[10] desktop_agent/control/receipts.py:52 — ""parent_receipt_id": store.last_receipt_id(subject_type, subject_id),"
[11] desktop_agent/control/receipts.py:62 — "def chain(store: Store, subject_type: str, subject_id: str) -> list[dict]:"
[12] desktop_agent/control/store.py:204 — "def save_receipt(self, rec: dict) -> None:"
[13] desktop_agent/control/store.py:218 — "fh.write(json.dumps(rec, default=str) + "\n")"
[14] desktop_agent/control/launcher.py:234 — "result_label=UNVERIFIED, evidence=[f"worker:{run.worker_id}", f"head={head}","
[15] desktop_agent/control/verifier.py:80 — "result_label=VERIFIED if result["ok"] else FAILED, evidence=evidence,"
[16] desktop_agent/control/verifier.py:5 — "`verified` or `failed` on the control plane's evidence alone."
[17] docs/ARCHITECTURE_REVIEW_2026-10-08.md:342 — "Rules: a worker can only produce `unverified`; `verified` requires the"
[18] docs/ARCHITECTURE_REVIEW_2026-10-08.md:336 — "actor, result_label: verified|unverified|simulated|failed,"
[19] tests/test_core.py:58 — "def test_receipt_law(tmp_path: Path):"
[20] tests/test_core.py:60 — "with pytest.raises(ReceiptError):"

## Review
1. OK — cites AGENTS.md:34 and :36, the statement and scope match.
2. OK — AGENTS.md:36-40 lists origin/authorisation/evidence and the self-report rule.
3. OK — four labels at receipts.py:20 and AGENTS.md:43; unknown labels raise at :33.
4. OK — receipts.py:35 raises for worker + verified.
5. OK — evidence and actor/source checks at receipts.py:37 and :39.
6. OK — parent id at receipts.py:52 and chain at :62.
7. OK — store.py save_receipt inserts into SQLite and appends receipts.jsonl.
8. OK — launcher.py:234 uses UNVERIFIED with worker, head and session evidence.
9. OK — verifier.py:80 label depends on the verifier's result; docstring at :5.
10. OK — architecture review lines 336 and 342 match.
11. OK — test_core.py:58-60 shows the raises cases; the later chain assertions were read in the same function.
