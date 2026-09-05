#!/usr/bin/env bash
# PostToolUse hook: format + autofix the just-edited Python file with ruff.
#
# Claude Code passes the tool payload as JSON on stdin. We pull the edited file
# path, and if it's a .py file inside this repo, run `ruff format` then
# `ruff check --fix` on *that file only* (fast, no repo-wide churn).
#
# Two deliberate choices:
#   * `--unfixable F401` — an autofix after *each* edit would strip an import
#     added just before the code that uses it, and the next edit would then
#     reference a name that's gone. F401 is still *reported* (so CI still fails
#     on a genuinely unused import); it just isn't rewritten out from under an
#     in-progress edit.
#   * Lint findings are never fed back into the session. Mid-sequence a file
#     legitimately references not-yet-written names (F821), and reporting that
#     would prompt a "fix" for something about to be fixed. CI is the gate for
#     unfixable errors; this hook is only a formatter.
#
# Formatting is best-effort: a failure exits 0 so a hiccup never blocks an edit.
# The one exception is a missing toolchain, which is otherwise indistinguishable
# from success and would silently stop formatting anything.
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

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
project_dir=$PWD

# Resolve a relative path against the project dir, then stay inside the repo:
# an edit to a .py file in some other checkout must not be reformatted under
# *this* repo's ruff config.
case "$file" in
  /*) ;;
  *) file="$project_dir/$file" ;;
esac
case "$file" in
  "$project_dir"/*) ;;
  *) exit 0 ;;
esac

[ -f "$file" ] || exit 0

uv run ruff format "$file" >/dev/null 2>&1
fmt_status=$?
uv run ruff check --fix --unfixable F401 "$file" >/dev/null 2>&1 || true

# `ruff format` exits non-zero both when the file is mid-edit and unparseable
# (expected — stay quiet) and when the toolchain is gone (worth saying out loud).
# One extra probe tells them apart, and only on this already-failing path.
if [ "$fmt_status" -ne 0 ] && ! uv run ruff --version >/dev/null 2>&1; then
  echo "ruff-format hook: 'uv run ruff' is unavailable — .py files are NOT being formatted." >&2
  exit 1
fi

exit 0
