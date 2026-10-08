# Aria widget — one-time setup on the EliteDesk

The control plane runs as user `caoscare-1` (user systemd service
`desktop-agent.service`, `http://127.0.0.1:8477`). The desktop session
belongs to user `michaelos`, so the widget runs there and talks to the control
plane over the local API with a token. Nothing is shared through home
directories.

## Michael, once, as `michaelos`

```bash
# 1. the widget code (public repo)
git clone https://github.com/caosos/Desktop-Agent.git ~/Desktop-Agent
cd ~/Desktop-Agent && git checkout build/stage-1      # until merged to main

# 2. the control-plane token: ask the caoscare-1 side for the contents of
#    /home/caoscare-1/.config/desktop-agent/token and paste it once:
python3 -m widget.app --token '<paste token>'
#    (stored in ~/.config/desktop-agent/widget.json, mode 0600)

# 3. conversational Aria needs no key: the control plane runs Aria's
#    reasoning server-side (POST /v0/aria/chat, cheap structured calls on the
#    subscription, about a cent per exchange). A local Anthropic key is only
#    an alternative:  python3 -m widget.app --anthropic-api-key '<key>'
#    optional voice: add "openai_api_key": "<key>" to the same json file
#    and install alsa-utils for arecord.
```

Then `python3 -m widget.app` opens the widget. A GNOME custom shortcut running
`python3 -m widget.app --toggle` from `~/Desktop-Agent` shows/hides it.

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
