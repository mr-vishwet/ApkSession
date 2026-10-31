#!/usr/bin/env bash
# Point the Qoder skill at this folder so edits here are the live tool.
# Keeps a timestamped copy of whatever was installed, and is idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tool"
SKILL="$HOME/.qoder/skills/app-demo-recorder"

if [ -L "$SKILL" ]; then
  echo "already linked -> $(readlink "$SKILL")"
  [ "$(readlink "$SKILL")" = "$HERE" ] && exit 0
fi

if [ -d "$SKILL" ]; then
  backup="$SKILL.pre-link.$(date +%Y%m%d-%H%M%S)"
  mv "$SKILL" "$backup"
  echo "installed copy moved to $backup"
fi

ln -sfn "$HERE" "$SKILL"
echo "linked $SKILL -> $HERE"

python3 - "$SKILL" <<'PY'
import subprocess, sys
from pathlib import Path
cli = Path(sys.argv[1]) / "scripts/adrd.py"
out = subprocess.run([sys.executable, str(cli), "--help"], capture_output=True, text=True)
verbs = [l for l in out.stdout.splitlines() if "doctor," in l or "watch," in l]
print("cli reachable through the link:", "adrd" in out.stdout)
if not verbs:
    print("usage line:", out.stdout.splitlines()[1] if len(out.stdout.splitlines()) > 1 else "?")
PY
