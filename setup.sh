#!/usr/bin/env bash
# One-time setup: dependencies, the emulator AVD, then prove the tool works.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
SDK="$HOME/Library/Android/sdk"
fails=0
say() { printf '%-4s %s\n' "$1" "$2"; }

python3 -m pip install --quiet imageio-ffmpeg pillow 2>/dev/null || \
  say WARN "pip install imageio-ffmpeg pillow failed - check network or Python"

python3 - <<'PY' || fails=$((fails+1))
import sys
sys.path.insert(0, "tool/scripts")
from adrd.env import doctor
bad = [p for p in doctor() if not p.ok and p.name in ("adb", "ffmpeg", "ffmpeg filters", "pillow")]
for p in doctor():
    say("ok" if p.ok else "MISS", f"{p.name}: {p.detail}")
if bad:
    print("core dependencies missing"); sys.exit(1)
PY

if [ -x "$SDK/emulator/emulator" ] && [ -z "$("$SDK/emulator/emulator" -list-avds 2>/dev/null | grep demo35)" ]; then
  say ".." "creating AVD demo35 (API 35 arm64, Pixel 6)"
  printf 'no\n' | "$SDK/cmdline-tools/latest/bin/avdmanager" create avd -n demo35 \
    -k "system-images;android-35;default;arm64-v8a" -d pixel_6 >/dev/null 2>&1
  # avdmanager defaults the GPU off, which makes an 8 GB Mac crawl
  python3 - <<'PY'
import pathlib
c = pathlib.Path.home() / ".android/avd/demo35.avd/config.ini"
if c.exists():
    t = c.read_text()
    for k, v in {"hw.gpu.enabled": "yes", "hw.gpu.mode": "host", "hw.ramSize": "2048",
                 "hw.cpu.ncore": "4", "hw.keyboard": "yes"}.items():
        t = "\n".join(l for l in t.splitlines() if not l.startswith(k + "=")) + f"\n{k}={v}\n"
    c.write_text(t)
    print("ok   demo35 configured for host GPU")
PY
fi

say ".." "running the self-test (no phone needed)"
tool/tests/run.sh >/tmp/apksession-setup.log 2>&1 \
  && say PASS "self-test green - tool/ is safe to build on" \
  || { say FAIL "self-test broken - see /tmp/apksession-setup.log"; fails=$((fails+1)); }

[ "$fails" = 0 ] && echo "ready" || echo "problems above"
exit "$fails"
