"""Aria desktop widget (GTK4). The widget never does work: it issues
control-plane commands and renders control-plane state (IMPLEMENTATION_PLAN §2).

Run:  python3 -m widget.app [--toggle] [--url URL] [--token TOKEN]
A second invocation with --toggle shows/hides the running instance.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .aria import Aria  # noqa: E402
from .client import ApiError, Client, RefreshCoalescer, class_label, load_config, save_config  # noqa: E402
from . import voice  # noqa: E402

PANEL_PATH = "/"
PIDFILE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "aria-widget.pid"
STAGE_LABELS = {"DONE": "done", "BLOCKED": "blocked", "WAITING_OWNER": "awaiting owner", "IDLE": "idle"}

CSS = b"""
window { background: #f4f6f7; }
.title { font-weight: 700; letter-spacing: 2px; font-size: 13px; }
.chip { padding: 2px 8px; border-radius: 4px; font-family: monospace; font-size: 11px; background: #e2f0f1; color: #0c6b75; }
.chip-bad { background: #f9e1e1; color: #9b2c2c; }
.chip-warn { background: #f7ecd2; color: #8a5a00; }
.chip-ok { background: #dff3e6; color: #1f6b3a; }
.muted { color: #5a6670; font-size: 12px; }
.reply { font-size: 13px; }
.project { padding: 4px 0; border-bottom: 1px solid #d6dde2; }
"""


def ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = max(0, time.time() - ts)
    return f"{int(s)}s" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h"


class AriaWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, client: Client, aria: Aria, cfg: dict):
        super().__init__(application=app, title="Aria")
        self.client, self.aria, self.cfg = client, aria, cfg
        self._event_stop = threading.Event(); self._event_refresh = RefreshCoalescer()
        self._sse_connected = False
        self.connect("close-request", self._on_close)
        self.set_default_size(320, 440)
        self.set_resizable(False)
        self._build()
        GLib.timeout_add_seconds(5, self._poll_fallback)
        self._refresh_async()
        threading.Thread(target=self.client.follow_events, args=(self._on_event, 0, self._event_stop,
                         self._on_sse_connection), daemon=True).start()

    # ---- layout -----------------------------------------------------------
    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.set_child(root)
        head = Gtk.Box(spacing=8)
        t = Gtk.Label(label="ARIA", xalign=0); t.add_css_class("title"); head.append(t)
        self.state_chip = Gtk.Label(label="connecting"); self.state_chip.add_css_class("chip"); self.state_chip.add_css_class("chip-warn")
        head.append(Gtk.Box(hexpand=True)); head.append(self.state_chip)
        root.append(head)

        self.reply = Gtk.Label(label="What would you like me to work on?", xalign=0, wrap=True, wrap_mode=2)
        self.reply.add_css_class("reply"); self.reply.set_size_request(-1, 48)
        root.append(self.reply)

        row = Gtk.Box(spacing=6)
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Talk to Aria or type a command…")
        self.entry.connect("activate", self._on_submit)
        row.append(self.entry)
        self.mic = Gtk.Button(label="🎤"); self.mic.set_tooltip_text("Push to talk (hold)")
        gesture = Gtk.GestureClick(); gesture.connect("pressed", self._on_mic_down); gesture.connect("released", self._on_mic_up)
        self.mic.add_controller(gesture)
        if not voice.available(self.cfg):
            self.mic.set_sensitive(False); self.mic.set_tooltip_text(voice.unavailable_reason(self.cfg))
        row.append(self.mic)
        root.append(row)

        self.inbox_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        root.append(self.inbox_box)

        lbl = Gtk.Label(label="ACTIVE PROJECTS", xalign=0); lbl.add_css_class("muted"); root.append(lbl)
        self.projects_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        root.append(self.projects_box)

        self.workers_lbl = Gtk.Label(label="", xalign=0, wrap=True); self.workers_lbl.add_css_class("muted"); root.append(self.workers_lbl)

        foot = Gtk.Box(spacing=6)
        b = Gtk.Button(label="Open panel"); b.connect("clicked", self._open_panel); foot.append(b)
        self.pause_btn = Gtk.Button(label="Pause"); self.pause_btn.connect("clicked", lambda *_: self._control("pause")); foot.append(self.pause_btn)
        s = Gtk.Button(label="Stop"); s.add_css_class("destructive-action"); s.connect("clicked", lambda *_: self._control("stop")); foot.append(s)
        root.append(foot)

    # ---- state ------------------------------------------------------------
    def _refresh_async(self) -> bool:
        threading.Thread(target=self._refresh, daemon=True).start()
        return False

    def _poll_fallback(self) -> bool:
        if not self._sse_connected:
            self._refresh_async()
        return True

    def _on_event(self, _event: dict) -> None:
        delay = self._event_refresh.request(time.monotonic())
        if delay is None:
            return
        if delay > 0:
            GLib.timeout_add(max(1, round(delay * 1000)), self._queue_event_refresh)
        else:
            self._queue_event_refresh()

    def _queue_event_refresh(self) -> bool:
        self._event_refresh.dispatched()
        if not self._event_stop.is_set():
            GLib.idle_add(self._refresh_async)
        return False

    def _on_sse_connection(self, error: ApiError | None) -> None:
        self._sse_connected = error is None
        if error is not None and not self._event_stop.is_set():
            GLib.idle_add(self._refresh_async)

    def _on_close(self, *_args) -> bool:
        self._event_stop.set(); return False

    def _refresh(self) -> None:
        try:
            st = self.client.state()
        except ApiError as exc:
            GLib.idle_add(self._render_offline, str(exc))
            return
        GLib.idle_add(self._render, st)

    def _render_offline(self, msg: str) -> None:
        self._chip("Offline", "chip-bad"); self.workers_lbl.set_text(msg[:120])

    def _chip(self, text: str, cls: str = "") -> None:
        self.state_chip.set_text(text)
        for c in ("chip-bad", "chip-warn", "chip-ok"):
            self.state_chip.remove_css_class(c)
        if cls:
            self.state_chip.add_css_class(cls)

    def _render(self, st: dict) -> None:
        running = [t for t in st["tasks"] if t["status"] == "RUNNING"]
        if st["inbox"]:
            self._chip(f"Awaiting owner ({len(st['inbox'])})", "chip-warn")
        elif running:
            self._chip(f"Working ({len(running)})", "chip-ok")
        elif st.get("paused"):
            self._chip("Paused", "chip-warn")
        elif st["blocked"] and not st["next"]:
            self._chip("Blocked", "chip-bad")
        else:
            self._chip("Ready", "")
        self.pause_btn.set_label("Resume" if st.get("paused") else "Pause")
        self._clear(self.inbox_box)
        for d in st["inbox"][:1]:
            q = Gtk.Label(label="Needs you: " + d["question"], xalign=0, wrap=True); self.inbox_box.append(q)
            br = Gtk.Box(spacing=6)
            for opt in (d["options"] or ["yes", "no"])[:3]:
                b = Gtk.Button(label=opt); b.connect("clicked", lambda _b, did=d["decision_id"], a=opt: self._decide(did, a)); br.append(b)
            self.inbox_box.append(br)
        self._clear(self.projects_box)
        for p in st["projects"]:
            row = Gtk.Box(spacing=6); row.add_css_class("project")
            name = Gtk.Label(label=p["name"], xalign=0, hexpand=True); row.append(name)
            stage = p["stage"]; chip = Gtk.Label(label=STAGE_LABELS.get(stage, stage.lower())); chip.add_css_class("chip")
            if stage == "BLOCKED": chip.add_css_class("chip-bad")
            if stage == "WAITING_OWNER": chip.add_css_class("chip-warn")
            if stage == "DONE": chip.add_css_class("chip-ok")
            row.append(chip)
            self.projects_box.append(row)
            if p.get("last_activity"):
                la = Gtk.Label(label=f"{p['last_activity'][:60]} · {ago(p.get('last_activity_at'))}", xalign=0, wrap=True); la.add_css_class("muted")
                self.projects_box.append(la)
        cost = st["costs"]
        line = f"{len(st['workers'])} worker(s) · slots {st['slots']['slots']} · today ${cost['today_usd']:.2f}"
        classes = sorted({class_label(st, t.get("model_class")) for t in running if t.get("model_class")})
        self.workers_lbl.set_text(line + (" · " + ", ".join(classes) if classes else ""))

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        while (c := box.get_first_child()) is not None:
            box.remove(c)

    # ---- actions ----------------------------------------------------------
    def _on_submit(self, *_):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text(""); self.reply.set_text("…")
        threading.Thread(target=self._ask, args=(text,), daemon=True).start()

    def _ask(self, text: str) -> None:
        try:
            reply = self.aria.ask(text)
        except Exception as exc:  # the widget must never die on a bad reply
            reply = f"Error: {exc}"
        GLib.idle_add(self.reply.set_text, reply[:400]); GLib.idle_add(self._refresh_async)

    def _control(self, action: str) -> None:
        if action == "pause" and self.pause_btn.get_label() == "Resume":
            action = "resume"
        threading.Thread(target=lambda: (self._safe(lambda: self.client.control(action)), GLib.idle_add(self._refresh_async)), daemon=True).start()

    def _decide(self, decision_id: str, answer: str) -> None:
        threading.Thread(target=lambda: (self._safe(lambda: self.client.decide(decision_id, answer)), GLib.idle_add(self._refresh_async)), daemon=True).start()

    def _safe(self, fn):
        try:
            return fn()
        except ApiError as exc:
            GLib.idle_add(self.reply.set_text, str(exc)[:200])

    def _open_panel(self, *_):
        Gtk.show_uri(self, f"{self.client.url}{PANEL_PATH}?access_token={self.client.token}", 0)

    def _on_mic_down(self, *_):
        self._chip("Listening", "chip-warn"); voice.start_recording()

    def _on_mic_up(self, *_):
        self._chip("Thinking", "")
        def work():
            text = voice.stop_and_transcribe(self.cfg)
            if text:
                GLib.idle_add(self.entry.set_text, text); GLib.idle_add(self._on_submit)
            else:
                GLib.idle_add(self._refresh_async)
        threading.Thread(target=work, daemon=True).start()


class AriaApp(Gtk.Application):
    def __init__(self, cfg: dict):
        super().__init__(application_id="com.caos.desktopagent.aria")
        self.cfg = cfg; self.win = None

    def do_activate(self):
        if self.win is None:
            provider = Gtk.CssProvider(); provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(self.win_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            client = Client(self.cfg["url"], self.cfg["token"])
            self.win = AriaWindow(self, client, Aria(client, self.cfg.get("anthropic_api_key", "")), self.cfg)
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._toggle)
        self.win.present()

    def win_display(self):
        from gi.repository import Gdk
        return Gdk.Display.get_default()

    def _toggle(self, *_):
        if self.win.is_visible():
            self.win.set_visible(False)
        else:
            self.win.present()
        return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aria-widget")
    ap.add_argument("--toggle", action="store_true", help="show/hide a running widget")
    ap.add_argument("--url"); ap.add_argument("--token"); ap.add_argument("--anthropic-api-key")
    args = ap.parse_args(argv)
    cfg = load_config()
    changed = False
    for k, v in (("url", args.url), ("token", args.token), ("anthropic_api_key", args.anthropic_api_key)):
        if v:
            cfg[k] = v; changed = True
    if changed:
        save_config(cfg)
    if args.toggle and PIDFILE.exists():
        try:
            os.kill(int(PIDFILE.read_text()), signal.SIGUSR1)
            return 0
        except (OSError, ValueError):
            pass
    if not cfg["token"]:
        print("No control-plane token. Run once with --token <token from ~/.config/desktop-agent/token on the control-plane user>.", file=sys.stderr)
        return 2
    PIDFILE.write_text(str(os.getpid()))
    try:
        return AriaApp(cfg).run([])
    finally:
        PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
