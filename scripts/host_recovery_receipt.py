#!/usr/bin/env python3
"""One consolidated host recovery receipt for the three owner surfaces (stdlib only, read-only, no model).

Prints what is actually running on this host right now and writes the same facts as JSON under
<data_dir>/recovery/<utc-stamp>.json so a before/after pair proves a controlled stop/start or a reboot:
unit state, process identity, loaded revision, HTTP health, listeners, intake cursors/pending counts, coordinator
resolution. Nothing here is inferred from GitHub or from an older comment. Run as the control-plane account.

    python3 scripts/host_recovery_receipt.py            # text + JSON file
    python3 scripts/host_recovery_receipt.py --json     # JSON only, to stdout
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
DATA = HOME / ".local/share/desktop-agent"
TOKEN_FILE = HOME / ".config/desktop-agent/token"
SURFACES = {                     # port → (project, what, launch owner as far as this account can see)
    8477: ("desktop_agent", "control plane + panel", "systemd --user desktop-agent.service (caoscare-1, linger)"),
    3000: ("caoscare", "frontend dev server", "systemd --user caoscare-frontend-dev.service (caoscare-1)"),
    8092: ("caoscare", "backend API", "manual start (login scope), see CAOSCare MICHAEL_ACTION_RUNBOOK §1"),
    8766: ("michael_business_os", "operator UI", "other account (michaelos, tmux); not visible from here"),
    8479: ("michael_business_os", "coordinator watchdog feed", "other account (michaelos, tmux); not visible from here"),
}


def sh(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<{type(exc).__name__}: {exc}>"


def unit(name: str) -> dict:
    out = sh(["systemctl", "--user", "show", name, "-p", "ActiveState,SubState,MainPID,ExecMainStartTimestamp,NRestarts,UnitFileState"])
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pid = int(d.get("MainPID") or 0)
    if pid:
        try:
            d["cwd"] = os.readlink(f"/proc/{pid}/cwd")
            d["cmdline"] = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()[:160]
            d["checkout_head"] = sh(["git", "-C", d["cwd"], "rev-parse", "HEAD"])   # the code loaded is the HEAD at ExecMainStartTimestamp
        except OSError as exc:
            d["cwd"] = f"<{exc}>"
    return d


def listener(port: int) -> dict:
    s = socket.socket(); s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", port)); open_ = True
    except OSError:
        open_ = False
    finally:
        s.close()
    owner = ""
    for line in sh(["ss", "-ltnp"]).splitlines():
        if f":{port} " in line:
            owner = line.split("users:")[-1].strip() if "users:" in line else "listening (owner not visible from this account)"
    return {"open": open_, "owner": owner}


def http(path: str, token: str | None = None) -> dict:
    t = time.time()
    try:
        req = urllib.request.Request("http://127.0.0.1:8477" + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        return {"status": 200, "ms": int((time.time() - t) * 1000), "body": json.loads(body) if body[:1] in (b"{", b"[") else None}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "ms": int((time.time() - t) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "error": str(exc)[:120], "ms": int((time.time() - t) * 1000)}


def main() -> int:
    now = datetime.now(timezone.utc)
    token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else None
    rec: dict = {"observed_utc": now.isoformat(timespec="seconds"),
                 "observed_chicago": now.astimezone(ZoneInfo("America/Chicago")).isoformat(timespec="seconds"),
                 "host": socket.gethostname(), "uptime_since": sh(["uptime", "-s"]),
                 "linger": {u: sh(["loginctl", "show-user", u, "-p", "Linger", "--value"]) for u in ("caoscare-1", "michaelos")},
                 "units": {n: unit(n) for n in ("desktop-agent", "caoscare-frontend-dev", "aria-wake")},
                 "listeners": {str(p): {"project": v[0], "what": v[1], "launch_owner": v[2], **listener(p)} for p, v in SURFACES.items()},
                 "health": http("/v0/health")}
    st = http("/v0/state", token) if token else {"status": None, "error": "no token file"}
    body = st.get("body") or {}
    ik = body.get("intake") or {}
    rec["intake"] = {"status": st.get("status"), "ms": st.get("ms"), "last_poll_at": ik.get("last_poll_at"), "last_error": ik.get("last_error"),
                     "counts": ik.get("counts"), "unacknowledged": ik.get("unacknowledged")}
    rec["coordinators"] = [{"project": p.get("name"), "coordinator": (p.get("coordinator") or {}).get("summary") or p.get("coordinator")}
                           for p in body.get("projects") or []]
    rec["tasks"] = {"running": [t["task_id"] for t in body.get("tasks") or [] if t.get("status") == "RUNNING"],
                    "ready": [t["task_id"] for t in body.get("tasks") or [] if t.get("status") == "READY"]}
    if "--json" in sys.argv:
        print(json.dumps(rec, indent=1, default=str)); return 0
    out_dir = DATA / "recovery"; out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (now.strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.write_text(json.dumps(rec, indent=1, default=str))
    da = rec["units"]["desktop-agent"]
    print(f"HOST RECOVERY RECEIPT  {rec['observed_utc']}  ({rec['observed_chicago']})  host {rec['host']}  booted {rec['uptime_since']}")
    print(f"linger: caoscare-1={rec['linger']['caoscare-1']} michaelos={rec['linger']['michaelos']}")
    print(f"desktop-agent: {da.get('ActiveState')}/{da.get('SubState')} enabled={da.get('UnitFileState')} pid={da.get('MainPID')} "
          f"since={da.get('ExecMainStartTimestamp')} restarts={da.get('NRestarts')} checkout_head={str(da.get('checkout_head', ''))[:12]} (code = HEAD at start time) cwd={da.get('cwd')}")
    print(f"health: {rec['health'].get('status')} in {rec['health'].get('ms')} ms; state: {rec['intake']['status']} in {rec['intake']['ms']} ms")
    for port, l in rec["listeners"].items():
        print(f"  :{port:<5} {'OPEN  ' if l['open'] else 'CLOSED'} {l['project']:<20} {l['what']:<26} launch: {l['launch_owner']}" + (f" [{l['owner']}]" if l["owner"] else ""))
    print(f"intake: last_poll_at={rec['intake']['last_poll_at']} error={rec['intake']['last_error']} counts={rec['intake']['counts']} unacknowledged={rec['intake']['unacknowledged']}")
    for c in rec["coordinators"]:
        print(f"  coordinator {c['project']}: {c['coordinator']}")
    print(f"tasks: running={rec['tasks']['running']} ready={rec['tasks']['ready']}")
    print(f"json: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
