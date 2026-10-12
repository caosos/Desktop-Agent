#!/usr/bin/env bash
# Coordinator test gate. Runs pytest and reports PASS/FAIL strictly from pytest's own exit status, which is
# captured BEFORE any output formatting (the 2026-10-10 defect was `pytest … | tail -1`, whose pipe hid a
# non-zero exit and let a failing tree be integrated). Usage: scripts/gate.sh [pytest args] — do not pass -q: pytest is
# already quiet and a second -q removes its own result line (the verdict stays correct; the count is then absent).
# Exit status = pytest's exit status. GATE_LOG=<file> keeps the full output.
set -u
cd "$(dirname "$0")/.." || exit 2
log="${GATE_LOG:-$(mktemp -t gate.XXXXXX.log)}"
"${GATE_PYTHON:-.venv/bin/python}" -m pytest -q "$@" >"$log" 2>&1
rc=$?                                         # captured first; nothing below can change it
# the summary is pytest's own result line, wherever it is; extra -q flags or warning blocks must not hide it
summary="$(grep -E '[0-9]+ (passed|failed|error|errors|skipped|xfailed|deselected)|no tests ran' "$log" | tail -n 1)"
[ -n "$summary" ] || summary="NO PYTEST SUMMARY LINE IN LOG"
verdict=FAIL; [ "$rc" -eq 0 ] && verdict=PASS
echo "GATE $verdict exit=$rc head=$(git rev-parse HEAD) load=$(cut -d" " -f1 /proc/loadavg) tree=$([ -z "$(git status --porcelain)" ] && echo clean || echo dirty) :: $summary (log $log)"
exit "$rc"
