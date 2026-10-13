# Pickup note — paused by Michael 2026-10-12 18:5x CDT ("cease all work, we are saving tokens")

Coordinator session at pause: `caoscare-1-c9` (pid 1653269). On resume, verify the registry name first
(`eligible_sessions()`), re-pin `config/projects/desktop_agent.yaml` `coordinator.session_name` and restart if it changed.

## Where things stand

- **Loaded = tested = `93f9f217909c`** (restart 18:46:01 CDT, MainPID 2012887, health 200). Repo `main` is docs-only beyond it.
- **Active item `da-c6d1e57152` (private personal memory): IMPLEMENTED + LOADED, not DONE.** Live and working: private store
  `~/.config/desktop-agent/private/memory.sqlite` (0700/0600, empty), `/memory` Memory Center, `/v0/memory/*`, Ask Aria
  retrieval (facts + core + "where we left off" appended to the private system context). Michael imports his own seed in the
  Memory Center; no test or coordinator touches the real store.
- **Remaining for DONE (four edits, refused by this session's auto-mode permission classifier):** see the diff below. After
  them: `scripts/gate.sh`, one `systemctl --user restart desktop-agent`, one live Ask Aria turn proving
  `/v0/memory/status` shows `exchanges_archived: 1` and `last_capture_at` set with nothing in `aria:history`/events, firsthand
  Claude Chrome look at the Memory Center on `:8477`, DONE receipt on Issue #3, reconcile BUILD_CHECKLIST + PROJECT_STATE.
  Unblock: Michael adds `Edit(//home/caoscare-1/Desktop-Agent/**)` and `Write(//home/caoscare-1/Desktop-Agent/**)` allow
  rules (`/permissions` or `~/.claude/settings.json`), or applies the diff by hand.
- **Queue after it:** `da-24b7dd665f` M1 dictation UX defect (ACK 6133417640; one transcript in the editable field, interim
  text distinct and corrected in place, nothing implicitly sent); then the Phase 5 browser operator (owner direction 17:4x CDT,
  OWNER_DIRECTIVES); `da-50b1e88f20` stays owner-gated (M3 paid provider, Daily maximum $0).
- **Receipts:** last coordinator receipts 6134690109 (WORKING, mirrored 6134709327) and the pause receipt named in
  PROJECT_STATE. No check-in is scheduled while paused; the next check-in is posted on resume.
- **Owner directions recorded today (OWNER_DIRECTIVES.md):** "Aria for life" (never forgets; one continuous conversation with
  gaps — `episodes.py` implements the archive and recall); the Desktop Agent gets Claude-in-Chrome-like browser capabilities.

## The four pending edits (apply verbatim)

```diff
--- desktop_agent/control/aria.py  (_converse, after self._persist() that follows the final "aria" history append)
+        try:                                                   # archive the exchange for life + capture durable facts (counts only); never breaks the reply
+            self.service.memory.after_turn(text, turn, reply, kind, [a["name"] for a in actions_taken], mem["ids"])
+        except Exception:
+            pass

--- desktop_agent/control/aria.py  (replace the whole greeting() body; aria_state.greeting already exists)
     def greeting(self) -> dict:
-        """Opening line from live state, no model call: decisions waiting, ..."""   (31 lines)
+        """Opening line from live state, no model call (aria_state.greeting)."""
+        return greeting(self.service.state())

--- desktop_agent/control/aria_api.py  (/v0/aria/status)
-        return service.aria.status() | {"dictation": {
+        return service.aria.status() | {"memory": service.memory.status(), "dictation": {

--- desktop_agent/control/llm.py  (_claude_cli): the system prompt goes through a 0600 private file, never argv
 from .config import RuntimeConfig
+from ..memory.safety import private_text_file
 ...
-        if system:
-            argv += ["--append-system-prompt", system]
+        sysf = private_text_file(self.cfg.private_dir, system) if system else None   # never on argv: any local process can read a command line
+        if sysf:
+            argv += ["--append-system-prompt-file", str(sysf)]
 ...
         except asyncio.TimeoutError:
             proc.kill()
             raise LLMError("claude_cli call timed out")
+        finally:
+            if sysf:
+                sysf.unlink(missing_ok=True)
 (shorten the module docstring by ~4 lines so llm.py stays at 300)
```

Also add the memory row to `docs/BLUEPRINT.md` (modules `desktop_agent/memory/*`, `panel/memory.html|js`, tests
`tests/test_memory_*.py`; status IMPLEMENTED) and, in `panel/index.html` `loadAria()`, one line reading `/v0/memory/status`
into the Aria model line (optional; the Memory Center shows the same metadata).

## Verification commands on resume

```bash
systemctl --user show desktop-agent -p MainPID -p ActiveEnterTimestamp
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8477/v0/health
scripts/gate.sh                      # never pipe pytest; do not pass -q
DA_LIVE=1 .venv/bin/python -m pytest tests/test_live_acceptance.py
```
