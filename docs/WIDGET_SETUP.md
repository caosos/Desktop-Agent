# Aria widget — one-time setup on the EliteDesk

**You do not need the widget.** The browser panel at `http://127.0.0.1:8477/`
(signed in with the token in `~/.config/desktop-agent/token`) is the complete
owner interface: the three project cards with drilldowns, the approval packet,
the Shared inbox, and **Ask Aria** (bottom-right bubble) with typed or dictated
messages. The desktop widget is optional: it only adds a small always-available
window in your GNOME session and push-to-talk through a separately metered
provider. Everything below is for that optional window.

The control plane runs as user `caoscare-1` (user systemd service
`desktop-agent.service`, `http://127.0.0.1:8477`). The desktop session
belongs to user `michaelos`, so the widget runs there and talks to the control
plane over the local API with a token. Nothing is shared through home
directories.

## Michael, once, in your own desktop login — the only step that needs you

The widget has to run inside your GNOME session, which the control plane's
account cannot reach. Everything else is automated. One command, once:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/caosos/Desktop-Agent/main/widget/install.sh)
```

It fetches the public repo into `~/Desktop-Agent`, adds an **Aria** launcher to
your applications menu, and starts the widget. On first start the widget asks
for a 6-character **pairing code**: press **Pair widget** on the panel
(http://127.0.0.1:8477/, where you are already signed in) and type the code
into the widget. The code is one-time and expires in 10 minutes; the widget
stores the resulting token itself (`~/.config/desktop-agent/widget.json`,
mode 0600). No token is ever copied by hand.

After that: open **Aria** from the applications menu, or bind a GNOME shortcut
to `python3 -m widget.app --toggle` (run from `~/Desktop-Agent`) to show/hide it.

Conversational Aria needs no key of yours: the control plane runs her reasoning
server-side (about a cent per exchange on the subscription). Optional: a local
Anthropic key (`--anthropic-api-key`) or, for push-to-talk, an OpenAI key in
the same json file plus `alsa-utils` for `arecord`.

Requirements already present on this host: Python 3.10, PyGObject with GTK 4.6.

## What the widget does

- Shows the control-plane state: `Ready · Listening · Thinking · Working (n)
  · Awaiting owner (n) · Paused · Blocked · Offline`, one row per project with
  its stage, the single next owner decision with answer buttons, worker count,
  slots and today's cost.
- Text box: talk to Aria in plain language. By default the control plane's
  Aria answers (status questions, explaining a task, submitting a goal after
  confirming it, answering a decision, pause/resume/stop); single-word
  `pause`, `resume`, `stop` act immediately without a model call. If the
  control plane is unreachable the widget falls back to direct-command mode
  (`in caoscare: <what to do>` asks for confirmation, then submits).
- Push-to-talk microphone when an OpenAI key and `arecord` are present.
- `Open panel` opens the web instrument panel in the browser with the token.

The widget never edits files or runs commands. Every command it sends is a
receipt on the control plane with actor `widget:aria`.

## Known limits

- GNOME on Wayland does not let an app pin itself above other windows; use
  the toggle shortcut.
- The widget refreshes from the control plane's SSE stream. While that stream
  cannot connect, it falls back to polling state every 5 seconds.
