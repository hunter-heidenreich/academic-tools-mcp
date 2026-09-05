#!/usr/bin/env bash
# PostToolUse hook: `ruff format` + `ruff check --fix` the just-edited .py file.
# Best-effort — failures exit 0 so a hiccup never blocks an edit. Lint findings
# are not surfaced to the session; CI is the gate for unfixable errors.
set -uo pipefail

payload=$(cat)

# python3 over jq: guaranteed present in a Python repo. Handles Edit/Write/MultiEdit.
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

# Stay inside this repo — another checkout's .py must not get this repo's config.
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
# --unfixable F401: an import added just before its first use is momentarily
# unused, and stripping it would break the next edit. Still reported, so CI
# catches a genuinely unused one.
uv run ruff check --fix --unfixable F401 "$file" >/dev/null 2>&1 || true

# Non-zero is either an unparseable mid-edit file (expected) or a missing
# toolchain, which is otherwise silent. Probe only here to tell them apart.
if [ "$fmt_status" -ne 0 ] && ! uv run ruff --version >/dev/null 2>&1; then
  echo "ruff-format hook: 'uv run ruff' is unavailable — .py files are NOT being formatted." >&2
  exit 1
fi

exit 0
