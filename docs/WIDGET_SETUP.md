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

# 3. optional: conversational Aria (otherwise direct-command mode works):
python3 -m widget.app --anthropic-api-key '<key>'
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
- Text box: in direct mode, `in caoscare: <what to do>` (or just the
  instruction when one project exists) asks for confirmation, then submits a
  goal; `status`, `pause`, `resume`, `stop` work as commands. In
  conversational mode (API key set) Aria uses the same control-plane tools and
  confirms before submitting.
- Push-to-talk microphone when an OpenAI key and `arecord` are present.
- `Open panel` opens the web instrument panel in the browser with the token.

The widget never edits files or runs commands. Every command it sends is a
receipt on the control plane with actor `widget:aria`.

## Known limits (Stage 1)

- GNOME on Wayland does not let an app pin itself above other windows; use
  the toggle shortcut.
- The widget polls state every 5 seconds (SSE follow is in the client and
  used by the panel; the widget will switch to it in Stage 2).
