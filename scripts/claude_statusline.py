#!/usr/bin/env python3
"""Claude Code status line for this host: shows the included-allowance windows in the terminal and,
from ONE designated session only, drops the same numbers for the Desktop-Agent control plane.

Input: the status-line JSON Claude Code pipes on stdin (docs: code.claude.com/docs/en/statusline).
Output: one line. Side effect (designated session only): atomic write of
``$DA_DATA_DIR/allowance.json`` (default ~/.local/share/desktop-agent) holding only
observed_at, source, version, session id/name, cwd and the ``rate_limits`` windows — nothing else from
the session. The designated session is the one whose launch directory equals $DA_ALLOWANCE_PROJECT_DIR
(default: the home directory, where the Desktop-Agent coordinator session runs). No network, no model.
"""
import json
import os
import sys
import time


def main() -> int:
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    model = (d.get("model") or {}).get("display_name") or "?"
    ctx = (d.get("context_window") or {}).get("used_percentage")
    rl = d.get("rate_limits") or {}
    parts = [f"[{model}]", f"ctx {round(ctx)}%" if isinstance(ctx, (int, float)) else "ctx —"]
    for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(name) or {}
        pct, resets = w.get("used_percentage"), w.get("resets_at")
        if pct is None:
            parts.append(f"{label} —")
        else:
            when = time.strftime("%a %H:%M" if name == "seven_day" else "%H:%M", time.localtime(resets)) if resets else "?"
            parts.append(f"{label} {round(pct)}% ↻{when}")
    print(" · ".join(parts))

    project_dir = (d.get("workspace") or {}).get("project_dir") or d.get("cwd") or ""
    designated = os.path.expanduser(os.environ.get("DA_ALLOWANCE_PROJECT_DIR", "~")).rstrip("/")
    if rl and project_dir.rstrip("/") == designated:
        data_dir = os.path.expanduser(os.environ.get("DA_DATA_DIR", "~/.local/share/desktop-agent"))
        drop = {"observed_at": time.time(), "source": "claude_code_statusline", "version": d.get("version"),
                "session_id": d.get("session_id"), "session_name": d.get("session_name"), "cwd": project_dir,
                "rate_limits": {k: {"used_percentage": v.get("used_percentage"), "resets_at": v.get("resets_at")}
                                for k, v in rl.items() if isinstance(v, dict) and k in ("five_hour", "seven_day")}}
        try:
            os.makedirs(data_dir, exist_ok=True)
            tmp = os.path.join(data_dir, "allowance.json.tmp")
            with open(tmp, "w") as f:
                json.dump(drop, f)
            os.replace(tmp, os.path.join(data_dir, "allowance.json"))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
