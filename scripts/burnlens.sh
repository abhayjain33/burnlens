#!/usr/bin/env bash
# Launcher for the /burnlens command and the SessionEnd hook.
#
# Finds a working Python 3.8+ (python3, python, or the Windows `py` launcher),
# skipping stubs such as the Windows Store "python3" alias, and always exits 0,
# printing any problem as normal output. Claude Code treats a non-zero exit
# from a slash command's shell step as a hard failure and hides the message.

# Windows passes C:\...\scripts\burnlens.sh; forward slashes work in both
# Git Bash and Windows Python, and avoid MSYS /c/... path translation.
script="${BASH_SOURCE[0]//\\//}"
run_py="$(dirname "$script")/run.py"

# Hooks and the status line must print clean JSON / one line and stay silent on failure.
hook=0
case "$1 $2" in
  "guard pre-tool" | "guard post-tool" | "guard prompt" | "statusline "*) hook=1 ;;
esac

for candidate in python3 python "py -3"; do
  # shellcheck disable=SC2086
  if command -v ${candidate%% *} >/dev/null 2>&1 &&
     $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    if [ "$hook" = 1 ]; then
      # shellcheck disable=SC2086
      $candidate "$run_py" "$@" 2>/dev/null
      exit 0
    fi
    # shellcheck disable=SC2086
    $candidate "$run_py" "$@" 2>&1
    status=$?
    if [ "$status" -ne 0 ]; then
      echo "burnlens stopped with exit status $status (see the message above)."
    fi
    exit 0
  fi
done

[ "$hook" = 1 ] && exit 0  # no Python: a hook must not print advice on every tool call

found=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    found="$found $candidate ($("$candidate" --version 2>&1 | head -1))"
  fi
done
echo "burnlens needs Python 3.8 or newer, and no working Python was found."
if [ -n "$found" ]; then
  echo "Found on PATH but not usable:$found"
  echo "On Windows, 'python3' and 'python' may be Microsoft Store placeholders rather than real installs."
else
  echo "No python3, python or py command is on PATH."
fi
echo "Install Python from https://www.python.org/downloads/ (Windows: tick 'Add python.exe to PATH'; macOS: 'xcode-select --install' also works), restart Claude Code, then run /burnlens again."
exit 0
