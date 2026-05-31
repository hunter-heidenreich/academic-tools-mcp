#!/usr/bin/env bash
# PostToolUse hook: format + autofix the just-edited Python file with ruff.
#
# Claude Code passes the tool payload as JSON on stdin. We pull the edited file
# path, and if it's a .py file under this repo, run `ruff format` then
# `ruff check --fix` on *that file only* (fast, no repo-wide churn). Everything
# is best-effort: any failure exits 0 so a formatting hiccup never blocks an edit.
set -uo pipefail

payload=$(cat)

# Extract the edited path. python3 is guaranteed present (this is a Python repo);
# avoids a hard jq dependency. Handles Edit/Write/MultiEdit payload shapes.
file=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input", {}) or {}
print(ti.get("file_path") or ti.get("filePath") or "")
' 2>/dev/null)

[ -z "$file" ] && exit 0
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

uv run ruff format "$file"     >/dev/null 2>&1 || true
uv run ruff check --fix "$file" >/dev/null 2>&1 || true
exit 0
