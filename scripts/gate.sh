#!/usr/bin/env bash
# Coordinator test gate. Runs pytest and reports PASS/FAIL strictly from pytest's own exit status, which is
# captured BEFORE any output formatting (the 2026-10-10 defect was `pytest … | tail -1`, whose pipe hid a
# non-zero exit and let a failing tree be integrated). Usage: scripts/gate.sh [pytest args]
# Exit status = pytest's exit status. GATE_LOG=<file> keeps the full output.
set -u
cd "$(dirname "$0")/.." || exit 2
log="${GATE_LOG:-$(mktemp -t gate.XXXXXX.log)}"
"${GATE_PYTHON:-.venv/bin/python}" -m pytest -q "$@" >"$log" 2>&1
rc=$?                                         # captured first; nothing below can change it
summary="$(tail -n 1 "$log")"
verdict=FAIL; [ "$rc" -eq 0 ] && verdict=PASS
echo "GATE $verdict exit=$rc head=$(git rev-parse HEAD) load=$(cut -d" " -f1 /proc/loadavg) tree=$([ -z "$(git status --porcelain)" ] && echo clean || echo dirty) :: $summary (log $log)"
exit "$rc"
