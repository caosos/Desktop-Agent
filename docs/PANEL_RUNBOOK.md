# Mission Control panel — owner runbook

`http://127.0.0.1:8477/?access_token=<token from ~/.config/desktop-agent/token>`

## The two views

- **Owner view** (default): one screen. Three project cards, today's top outcomes, decisions waiting, one NEXT action.
- **Details ▸** (button, top right): everything the control plane knows: at-a-glance strip, Goal form, Projects, Coordinators and workers, Cost, Blocked, Shared inbox history, Tasks, Live events. Nothing is removed by Owner view; it is only folded away.

## Words on the cards

| Word | Means | Does not mean |
|---|---|---|
| Project | the repo and its work | — |
| Coordinator | the Claude session (or liaison) steering the project; *idle* = waiting for a message | down, or that nobody is working |
| Dispatcher | Business OS's loop that launches its own workers | — |
| Workers | jobs running **now**, counted from live processes and the project's own feed | queue rows, status files, shell-command counts |
| IDLE | verified: nothing running now | the project is finished or stopped |
| DOWN | a coordinator session that should be reachable is not, or a dispatcher is stopped with work queued | an idle coordinator |
| not verified | no telemetry for that project; no claim is made | zero workers |
| ✓ | verified by the control plane (its own test gate in a clean checkout) | merged or shipped (that is a separate badge) |
| spinner | a worker process is alive right now | — |
| amber | an actual blocker or operational alert | a reminder about optional items |

## What the controls do

| Control | Effect | Receipt |
|---|---|---|
| **Open approval packet → Submit answers** | records your consent per item; blank = unanswered (never consent); "defer a day" hides without answering | one receipt per answered item, actor human, source panel |
| **Goal → Submit goal** (Details) | plans and starts a **new** bounded worker task; a model runs and its cost appears on the Cost card | goal + task receipts |
| **Shared inbox → Transfer / Send** (Details) | posts a direction on the project's owner-dispatch GitHub issue and delivers it to that project's existing coordinator; creates no task | instruction receipts, status chips POSTED → DELIVERED → ACKNOWLEDGED → DONE |
| **Pause / Resume** | stops / allows this control plane's scheduler starting new workers (running workers finish) | control receipt |
| **Stop all** | kills this control plane's running workers | control receipt |
| **Pair widget** (Details) | mints a one-time code for the optional desktop widget | pairing events |

Pause, Resume and Stop never touch CAOSCare's or Business OS's own workers.

## Ask Aria (bubble, bottom right)

- **What it is:** a text conversation with the control plane's Aria on the existing `/v0/aria/chat`. The header line names the model and backend in use and its cost note; when no model is permitted it says so and answers with a deterministic status only.
- **Kinds of message, shown as a chip on each reply:** *question* (answered read-only from live state, project briefs, task details: the reply names its source), *direction* (sent to the project's coordinator through the Shared inbox, tracked POSTED → DELIVERED → ACK → DONE; never finished work by itself), *new work* (a bounded worker task via the planner, confirmed first), *owner decision* (recorded in the packet, confirmed first), *control* (pause / resume / stop, confirmed first). Every action leaves a receipt and is listed under the reply.
- **Dictation (🎤 Dictate):** click to start, speak in complete thoughts with pauses, click **Stop**. States: Ready → Listening → Transcribing → Text ready, or an error that says what to do (permission denied, no speech engine, speech service unreachable). Text lands in the box; edit it; nothing is sent until you press **Send**. Audio goes to your browser's speech service (in Chrome: Google's servers), never to this panel or Aria; no recordings are kept. Needs Chrome, microphone permission on click, and a localhost or HTTPS page (your SSH tunnel qualifies). On-device speech is not installed (one owner decision); the metered OpenAI path stays off.
- **Transcript:** kept by the control plane (last 60 turns) so a reload or restart shows it again; "Ask Aria about this project" in a drilldown opens the bubble with that project named.

## Money

**Allowance** (Owner view row; also under Coordinators and workers in Details) = the included Claude subscription windows for this host's shared account: 5-hour and 7-day used %, each with its reset time, the observation time and its source (the coordinator session's own status line, or the last worker run). A window shows **UNKNOWN** with the reason when the source did not report it or its reset time has passed; **STALE** when the last observation is older than 30 minutes. It is the same number for every session of the account — never a sum — and it is not a bill.


Known dollars are token-equivalent list prices reported by the Claude CLI on the subscription, not an invoice. Anything the platform cannot price shows as **UNKNOWN**, never `$0`. Metered API invoices are impossible while OpenAI is DISABLED and no Anthropic key exists; both decisions are optional and stay unanswered until you submit them.
