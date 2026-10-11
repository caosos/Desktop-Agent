# Operator tools contract — desktop, browser, file and SSH operations (Phase 5, item 1)

**Status:** DESIGN, 2026-10-11. Nothing in this document is implemented; no tool here is granted to any worker or to
Aria. It defines the contracts, allowlists, action preview and authority so that item 2 of Phase 5 (one narrow desktop
action and one SSH action, end to end) can be built without re-deciding scope. It applies the architecture review's
settled decisions (`ARCHITECTURE_REVIEW_2026-10-08.md` §3 point 9, §4 row 10, §6 row 12, §11 Phase 2) and the owner's
direction (`OWNER_DIRECTIVES.md` 2026-10-08 point 5: "eventually operate the computer through governed tools"; point 8:
involve Michael only for owner decisions, credentials, unauthorised spend, destructive actions). Where this document
proposes something the review did not settle, the line says **PROPOSED**.

## 1. Settled before this document (not re-decided here)

| Decision | Source | Consequence for operator tools |
|---|---|---|
| Desktop control is a separate product surface sharing the control plane, event log and receipts; its tools are a **separate tool class that coding workers can never be granted** | review §3 point 9, §6 row 12 | One tool class `operator`; `allowed_tools` of a coding contract may never contain an operator tool; the launcher refuses such a contract (not a warning). |
| Safe Linux desktop control = **at-spi2 accessibility tree** (semantic) first, **ydotool** (uinput, Wayland) for input, screenshots via portal, **Playwright** for browsers; a **separate MCP tool server with its own policy, a panic hotkey that kills the scope, session recording as evidence** | review §4 row 10 | The operator server is its own process and policy file; every session is recorded; one key stops everything. |
| Governed tools reach workers through **one MCP server owned by the control plane** | review §4 row "tool-governance protocol" | Operator tools are MCP tools on a *second* server (`operator`), never mixed into the coding server. |
| Receipt law: no action without a receipt, no receipt without provenance; result labels `verified` / `unverified` / `simulated` / `failed` | AGENTS.md | Every operator action is a receipt with its own observation of the result; the agent's "I clicked it" is `unverified` until the server observed the effect. |
| Owner-decision taxonomy | OWNER_DIRECTIVES point 8/9 | Destructive, credential, spend and production actions escalate; routine inspection does not. |

## 2. Host facts that shape the first implementation (observed 2026-10-11, read-only)

- The desktop session (`seat0`, tty2) belongs to user **`michaelos`**; the control plane runs as **`caoscare-1`**. Nothing
  in `caoscare-1` can read `/run/user/1001` (mode 0700) or inject input into that session. **Any desktop tool server must
  run inside Michael's own login** (same place the optional GTK widget runs, `WIDGET_SETUP.md`) and talk to the control
  plane over the local API with a paired token. The control plane never gains cross-account access.
- GNOME runs on **Wayland** (`/usr/bin/Xwayland :2 -rootless`, `gnome-shell`); `at-spi2-registryd` is running. `xdotool`
  is installed but reaches only Xwayland clients; `ydotool`/`ydotoold`, `grim`, `gnome-screenshot`, `wl-paste` are **absent**;
  `/dev/uinput` is root-only. Python `gi.repository.Atspi` imports (the semantic tree is reachable from a process inside
  the session).
- `ssh` client is present; no `tailscale`, no `sshpass`. Playwright with the system Chrome is already the platform's
  browser harness (`tests/test_panel_browser.py`).
- Consequence: the first desktop action should be **read-only through at-spi** (no input injection needed, no new
  software), the first SSH action **read-only against a host Michael names**, and the first browser action
  **Playwright in a dedicated profile**. Input injection on Wayland needs `ydotool` + a udev rule for `/dev/uinput`
  (new software and a root change → owner decision, §7).

## 3. Tool classes and the authority ladder

Every operator tool belongs to exactly one class; every class has one authority level; the level decides what happens
before the tool runs. Levels never widen at runtime.

| Class | Tools (MCP names, PROPOSED) | Level | What the level means |
|---|---|---|---|
| `observe.desktop` | `desktop.tree` (at-spi subtree of a named window), `desktop.windows` (titles, apps), `desktop.screenshot` (portal; redaction rule §5) | **L1 observe** | Runs without a per-action confirmation inside an *active operator session* (§4). Receipt per call. |
| `observe.browser` | `browser.open(url ∈ allowlist)`, `browser.read` (text/accessibility of the page), `browser.screenshot` | L1 observe | Same; the URL allowlist is per session. |
| `observe.file` | `file.read`, `file.list` inside the session's path allowlist (never `never_read` patterns) | L1 observe | Same. |
| `observe.ssh` | `ssh.run_readonly(host ∈ allowlist, cmd ∈ read-only command allowlist)` | L1 observe | Same; the command allowlist is literal prefixes (`ss -ltn`, `systemctl --user status …`, `df -h`, `uptime`, `git -C <path> log …`), nothing with a pipe, redirect or `sudo`. |
| `act.desktop` | `desktop.click(path)`, `desktop.type(text)` by at-spi action or ydotool | **L2 preview+confirm** | The exact action is shown (target, window, text) and Michael confirms **that action**; one confirmation per action, no batch. |
| `act.browser` | `browser.click`, `browser.fill`, `browser.submit` (never a payment or credential field, §6) | L2 preview+confirm | Same. |
| `act.file` | `file.write`, `file.move` inside the allowlist; never delete | L2 preview+confirm | Diff shown before confirm. |
| `act.ssh` | `ssh.run(host, cmd)` outside the read-only list | L2 preview+confirm | The literal command and host are shown; confirm per command. |
| `destructive.*` | anything that deletes, force-pushes, restarts someone else's service, changes accounts, pays, sends a message as Michael | **L3 owner decision** | Goes to the approval packet as a decision with options; never executed from a confirmation prompt. |

Aria's conversation layer may *propose* any level; it may *run* L1 only inside an active session, L2 only after the
per-action confirmation, L3 never (it files the decision). Coding workers have no access to any class; the launcher
rejects a contract whose `allowed_tools` names an operator tool (`tool_class_violation`, receipt `failed`).

## 4. Operator session: the unit of authority

An operator session is the only context in which operator tools run. It is created by Michael (panel or widget, or by
Aria on his explicit request) and carries:

- `session_id`, `started_by` (human, source), `started_at`, `expires_at` (default **30 min**, PROPOSED, extendable only by
  Michael), `purpose` (his words).
- **Targets**: window-title patterns or app ids for desktop; URL allowlist for browser; path allowlist for files; host
  allowlist for SSH (`user@host`, key path, no passwords). Empty lists mean *nothing*; there is no `*`.
- **Level ceiling**: `observe` or `act`. `destructive` is never a session ceiling.
- **Recording**: every call's request, preview, confirmation, result and observation appended to the event log under
  `task_id = session_id`, plus the screenshot/tree snapshot taken *after* each `act` call as the observation of effect.
- **Panic**: one hotkey in Michael's session (PROPOSED `Ctrl+Alt+Shift+Esc`) and the panel's Stop both send `operator.stop`;
  the server kills its own scope, ends the session, writes one `failed`/`cancelled` receipt for any in-flight action.
- **Expiry**: at `expires_at` the session ends and every further call is refused with the reason; nothing is extended
  silently.

A session is not a grant to do the task; it is a grant to *use these tools on these targets at this level until then*.

## 5. Visible action preview (what Michael sees before an L2 action)

One card, same shape every time, in the Ask Aria drawer and on the widget:

```
ACTION  desktop.click
WHERE   window "Firefox — Deal Desk"  ·  element: button "Save draft" (at-spi path /frame[0]/toolbar/button[3])
WHY     Aria: "you asked me to save the draft before leaving the page"
RESULT WILL BE OBSERVED BY  at-spi tree + screenshot after the click
UNDO    none automatic — the page's own undo
[Do it]  [Skip]  [Stop session]
```

Rules: the preview names the **exact target** (never "the button"), the **observation method**, and whether an **undo**
exists. Confirmation is per action; "do all" does not exist. Skipping records a receipt (`skipped by owner`). For
`browser.fill`, text that looks like a credential, card or government id is refused at the preview stage (§6), not
sent for confirmation. Screenshots in previews and recordings mask password fields by their accessibility role.

## 6. Hard limits (never, regardless of session or confirmation)

- No typing or filling of passwords, API keys, tokens, card or bank numbers, government ids; no creating accounts; no
  CAPTCHA solving. The server refuses by field role and by pattern; the refusal is a receipt.
- No payments, transfers, bids, purchases, seller contact, or messages sent as Michael (the owner's 2026-10-11 wording
  for Deal Sniffer applies to every project).
- No action on CAOSCare's live room node services (`:3000`, `:8092`, Mongo `caoscare`) and no change to another
  account's processes; SSH `act` to those targets is `destructive` → owner decision.
- No `sudo`, no package installation, no permission or policy edits by any tool.
- No operator tool is ever granted to a coding worker; no coding tool (Edit/Bash) is granted to the operator server.
- Nothing is reported as done without an observation. If the post-action tree/screenshot does not show the expected
  change, the receipt says `unverified: effect not observed` and the next action waits.

## 7. Dependencies and owner decisions before item 2 can run

| Need | Why | Who |
|---|---|---|
| A process in Michael's GNOME login to host the operator MCP server (same bootstrap as the widget: `widget/install.sh` and pairing code) | cross-account boundary (§2) | Michael runs the installer once; nothing else possible from `caoscare-1` |
| First read-only desktop action target: one window/app he names | allowlist has no `*` | Michael names it |
| First SSH target: `user@host` he names, key-based, read-only command list | allowlist has no `*` | Michael names it (Tailscale remains a separate open decision from review §12) |
| Input injection on Wayland (`ydotool` + `/dev/uinput` udev rule) | new software + root change | owner decision; **not needed** for item 2 if the first action is at-spi read-only + one at-spi `do_action` on a button (at-spi can trigger a button's action without synthetic input — PROPOSED first `act` path, no new software) |
| Browser profile for `observe.browser`/`act.browser` | a dedicated Chrome profile without his saved logins | control plane creates it under his login via the operator server; no credentials copied |

## 8. What item 2 should build (bounded, in this order)

1. `desktop_agent/operator/` server skeleton (runs under Michael's login, pairs like the widget, speaks MCP to Aria's
   conversation layer only): `operator.session.start/stop`, `desktop.windows`, `desktop.tree`, `desktop.screenshot`
   (L1), with recording and expiry; **tests with a fake at-spi tree**; one live run reading one window Michael names.
2. `ssh.run_readonly` against the host he names with the literal command allowlist; observation = the command's own
   output + exit code; one live run.
3. One `act.desktop` action through at-spi `do_action` on a button in that window with preview → confirm → observe →
   receipt; one deliberately denied action (a password field) and one cancelled action (panic) recorded truthfully.
4. Panel/drawer: operator session card (targets, level, expiry, recording link), the preview card (§5), Stop.
5. Only then: browser tools, file tools, input injection decision.

Each step is one bounded task with a verified receipt before the next starts; a self-reported "clicked" is never DONE.

## 9. Open questions for Michael (one question each, with the recommendation)

1. **First desktop target**: which window/app should the first read-only action inspect? Recommendation: the Deal Desk
   browser window, read-only, so the first live proof is on his real use case.
2. **First SSH target**: which host and user, key-based? Recommendation: the EliteDesk itself from his Lenovo is *not* a
   useful proof (same machine); a host he already manages by hand is.
3. **Panic hotkey** and **session length**: accept `Ctrl+Alt+Shift+Esc` and 30 minutes, or name others.

Until these are answered this document is the deliverable for Phase 5 item 1; item 2 does not start.
