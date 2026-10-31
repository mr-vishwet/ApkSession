#!/usr/bin/env bash
# End-to-end self-test with no phone attached. Usage: tests/run.sh [workdir]
set -uo pipefail
SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$SKILL/.." && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
ADRD="$PY $SKILL/scripts/adrd.py"
WORK="${1:-$(mktemp -d /tmp/adrd-selftest.XXXX)}"
FIX="$WORK/fixture_app"
SESS=""
fails=0

check() { if [ "$1" = "0" ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi; }

mkdir -p "$FIX/lib/features/auth" "$FIX/lib/features/home" "$FIX/lib/core"
cat > "$FIX/pubspec.yaml" <<'Y'
name: fixture_app
Y
mkdir -p "$FIX/android/app"
cat > "$FIX/android/app/build.gradle.kts" <<'Y'
android { defaultConfig { applicationId = "com.client.fixture" } }
Y
cat > "$FIX/lib/core/routes.dart" <<'Y'
class AppRoutes {
  static const String login = '/login';
  static const String home = '/home';
}
Y
cat > "$FIX/lib/main.dart" <<'Y'
class MyApp extends StatelessWidget {
  Widget build(BuildContext c) => MaterialApp.router(routerConfig: GoRouter(
    initialLocation: '/login',
    routes: [
      GoRoute(path: '/login', name: 'login', builder: (c, s) => const LoginScreen()),
      GoRoute(path: '/home', name: 'home', builder: (c, s) => const HomeScreen()),
    ]));
}
Y
cat > "$FIX/lib/features/auth/login_screen.dart" <<'Y'
class LoginScreen extends StatelessWidget {
  void submit(BuildContext c) => context.go(AppRoutes.home);
}
class SplashScreen extends StatelessWidget {}
Y
cat > "$FIX/lib/features/home/home_screen.dart" <<'Y'
class HomeScreen extends StatelessWidget {
  void logout(BuildContext c) => Navigator.of(c).pushNamed('/login');
}
Y

echo "== flow =="
SESS=$($ADRD new --project "$FIX" --out "$WORK/sessions" | tail -1)
$ADRD flow --session "$SESS" > /dev/null
"$PY" - "$SESS" <<'PY'
import json, sys
d = json.load(open(f"{sys.argv[1]}/flow_map.json"))
routes = {r["route"]: r for r in d["routes"]}
mods = {r["route"]: r["module"] for r in d["routes"]}
edges = {(e["to"], e["resolved"]) for e in d["edges"]}
assert d["package"] == "com.client.fixture", d["package"]
assert set(routes) == {"/login", "/home"}, sorted(routes)
assert mods["/login"] == "auth" and mods["/home"] == "home", mods
assert ("/home", True) in edges and ("/login", True) in edges, edges
print("  ok   routes, modules, resolved edges, package id")
PY
check $? "flow assertions" || true
$ADRD scaffold --session "$SESS" > /dev/null; check $? "walk skeleton from the flow map"

echo "== synthetic take =="
"$PY" "$SKILL/tests/synthetic_take.py" "$SESS" "$WORK/clips" > /dev/null
check $? "generated 60s fake recording + events"
OUT=$($ADRD retimeline --session "$SESS" | grep -o 'offset [0-9.]*s residual [0-9.-]*s')
echo "  .. $OUT"
"$PY" - "$SESS" <<'PY'
import json, sys
t = json.load(open(f"{sys.argv[1]}/timeline.json"))
assert abs(t["offset"] - 1.0) < 0.05, f"offset {t['offset']} should recover 1.0s"
assert t["align"]["cost"] < 0.05, t["align"]
assert len(t["chapters"]) == 6, len(t["chapters"])
assert [c["module"] for c in t["chapters"]] == ["splash","login","signup","otp","home","reports"]
assert all(abs(c["t0"] - 10*i) < 0.05 for i, c in enumerate(t["chapters"])), t["chapters"]
print("  ok   clock aligned, 6 chapters on the true screen cuts")
PY
check $? "alignment assertions" || true

echo "== compressed video clock =="
# Android's screenrecord encodes on invalidation, so dwell time never reaches the file.
# A 10s-per-screen walk recorded into 3s-per-screen clips must still land its chapters.
S2=$($ADRD new --project "$FIX" --out "$WORK/sessions2" | tail -1)
"$PY" "$SKILL/tests/synthetic_take.py" "$S2" "$WORK/clips2" --compressed > /dev/null
$ADRD retimeline --session "$S2" > "$WORK/comp.log" 2>&1
"$PY" - "$S2" <<'PY'
import json, sys
t = json.load(open(f"{sys.argv[1]}/timeline.json"))
got = [c["t0"] for c in t["chapters"]]
want = [0, 3, 6, 9, 12, 15]
assert t["align"]["method"] == "cut anchored", t["align"]
# scale is measured (video/wall) so it lands near but not exactly on the per-screen
# ratio - the chapters are the thing that must be exact.
assert 0.25 < t["align"]["scale"] < 0.40, t["align"]
assert len(got) == 6 and all(abs(a - b) < 0.2 for a, b in zip(got, want)), (got, want)
assert t["align"]["residual"] < 1.0, t["align"]
print(f"  ok   chapters recovered at {got} despite a {t['align']['scale']:.2f}x clock")
PY
check $? "compressed-clock assertions" || true
grep -qE "video is [0-9]+% of wall time" "$WORK/comp.log" \
  && echo "  ok   compression disclosed to the user" \
  || { echo "  FAIL compression not disclosed"; fails=$((fails+1)); }

echo "== branding + render =="
for s in splash login signup otp home reports; do
  $ADRD brand add --session "$SESS" --screen "$s" --rect 160,105,560,140 \
    --label "app wordmark" --note "client name" > /dev/null
done
$ADRD brand add --session "$SESS" --screen login --rect 40,100,150,130 --mode pixelate \
  --label "brand glyph" > /dev/null
$ADRD brand preset --session "$SESS" --name debug-banner status-bar > /dev/null
$ADRD sheet --session "$SESS" --cell-h 300 > /dev/null; check $? "annotated screen sheet"
$ADRD render --session "$SESS" --step-captions --clock > "$WORK/render.log" 2>&1
check $? "render"
$ADRD sweep --session "$SESS" --height 420 > /dev/null; check $? "sweep"
$ADRD doc --session "$SESS" > /dev/null; check $? "doc"
grep -q "unchanged - box is wrong" "$WORK/render.log" && check 1 "no mispositioned box" || check 0 "no mispositioned box"

"$PY" - "$SESS" <<'PY'
import json, sys
from pathlib import Path
S = Path(sys.argv[1]); r = json.load(open(S / "render.json"))
v = Path(r["video"])
assert v.exists() and v.stat().st_size > 100_000, v
assert Path(r["vtt"]).exists() and Path(r["proof"]["sheet"]).exists()
assert (S / "render/sweep.png").exists()
tally = r["proof"]["tally"]
assert tally.get("unchanged - box is wrong", 0) == 0, tally
assert tally.get("strongly reduced - confirm by eye", 0) >= 12, tally
doc = (S / "render/SESSION_DOC.md").read_text()
assert "| 1 | splash | `00:00` |" in doc, doc[:600]
assert "Shareable video" in doc and "never send this one" in doc
assert len([l for l in doc.splitlines() if l.startswith("| `0")]) > 15
print(f"  ok   mp4+vtt+proof+sweep+doc, tally {tally}")
PY
check $? "output assertions" || true

echo "== dashboard page script =="
"$PY" "$SKILL/tests/check_page_js.py"; check $? "dashboard script parses"

echo "== dashboard =="
PORT=$(( 8900 + RANDOM % 80 ))
# the in-process path: render --watch must serve, print a URL, and linger, not hang
$ADRD render --session "$SESS" --watch --port $PORT --no-browser --keep-alive 1 \
  > "$WORK/watched.log" 2>&1
check "$([ "$(grep -c 'dashboard http' "$WORK/watched.log")" = 1 ] && echo 0 || echo 1)" \
  "render --watch served the dashboard and returned"
$ADRD watch --session "$SESS" --port $PORT --lan --no-browser > "$WORK/watch.log" 2>&1 &
WPID=$!
sleep 1.5
TOK=$(grep -o 't=[A-Za-z0-9_-]*' "$WORK/watch.log" | head -1 | cut -d= -f2)
B="http://127.0.0.1:$PORT"
N=$(basename "$SESS")

# expect <desc> <want-code> <url>
expect() {
  got=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$3")
  if [ "$got" = "$2" ]; then echo "  ok   $1"; else echo "  FAIL $1 (got $got want $2)"; fails=$((fails+1)); fi
}
expect_len() {
  n=$(curl -s -m 5 "$3" | grep -c "$2")
  if [ "$n" -ge 1 ]; then echo "  ok   $1"; else echo "  FAIL $1 (no match for $2)"; fails=$((fails+1)); fi
}
expect "state endpoint with token"        200 "$B/s/$N/state?t=$TOK"
expect "lan bind rejects no token"        401 "$B/s/$N/state"
expect "lan bind rejects bad token"       401 "$B/s/$N/state?t=wrong"
expect "session index"                    200 "$B/?t=$TOK"
expect "index also needs the token"       401 "$B/"
expect "dashboard page"                   200 "$B/s/$N/?t=$TOK"
expect "artifact download"                200 "$B/s/$N/f/render/SESSION_DOC.md?t=$TOK"
expect "video streams"                    200 "$B/s/$N/f/render/demo_debranded.mp4?t=$TOK"
expect_traversal() {
  got=$(curl -s -m 5 --path-as-is -o /dev/null -w '%{http_code}' "$1")
  if [ "$got" = "403" ]; then echo "  ok   path traversal blocked"; else echo "  FAIL traversal (got $got)"; fails=$((fails+1)); fi
}
expect_traversal "$B/s/$N/f/../../../../../../etc/passwd?t=$TOK"
expect_len "state carries chapters+boxes" '"chapters"' "$B/s/$N/state?t=$TOK"
got=$(curl -s -m 5 -r 10-20 -o /dev/null -w '%{http_code}' "$B/s/$N/f/render/demo_debranded.mp4?t=$TOK")
if [ "$got" = 206 ]; then echo "  ok   byte-range seeking"; else echo "  FAIL byte-range seeking (got $got)"; fails=$((fails+1)); fi
"$PY" - "$B" "$N" "$TOK" "$SESS" <<'PYV'
import json, sys, time, urllib.request, pathlib
B, N, TOK, sess = sys.argv[1], sys.argv[2], sys.argv[3], pathlib.Path(sys.argv[4])
d = json.load(urllib.request.urlopen(f"{B}/s/{N}/state?t={TOK}", timeout=5))
arts = {a["name"]: a["what"] for a in d["artifacts"]}
assert arts.get("demo_debranded.mp4") == "shareable video", arts
assert "render.json" not in arts, arts
assert d["stage"] in ("verifying", "rendering", "ready to share"), d["stage"]

# a status file older than the live window must not be presented as a running task
st = sess / "status.json"
fresh = json.loads(st.read_text())
st.write_text(json.dumps({**fresh, "at": time.time() - 600}))
old = json.load(urllib.request.urlopen(f"{B}/s/{N}/state?t={TOK}", timeout=5))
assert old["step"] == {}, f"stale step leaked: {old['step']}"
assert old["stage"] != fresh["stage"], f"stale stage leaked: {old['stage']}"
st.write_text(json.dumps({**fresh, "at": time.time()}))
print("  ok   artifact labels correct, stale live status expires")
PYV
check $? "state semantics" || true

got=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$B/s/$N/stream?t=$TOK")
if [ "$got" = 200 ]; then echo "  ok   live view falls back to a still with no device"; else echo "  FAIL live view fallback (got $got)"; fails=$((fails+1)); fi

echo "== ingest chunk endpoints =="
# the routes live in the same server this watch process is running, so exercise them here
"$PY" - "$B" "$N" "$TOK" "$SESS" <<'PYI'
import hashlib, json, pathlib, sys, urllib.request
B, N, TOK, sess = sys.argv[1], sys.argv[2], sys.argv[3], pathlib.Path(sys.argv[4])
src = bytes((i * 37 + 11) & 0xFF for i in range(3000))
CS = 1024

def call(method, path, body=None, raw=None):
    data = json.dumps(body).encode() if body is not None else raw
    req = urllib.request.Request(B + path + f"?t={TOK}", data=data, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")

code, out = call("POST", f"/ingest/{N}/init",
                 {"filename": "clip.bin", "size": len(src),
                  "sha256": hashlib.sha256(src).hexdigest(), "chunk_size": CS})
assert code == 200, out
uid = out["upload_id"]
again = call("POST", f"/ingest/{N}/init",
             {"filename": "clip.bin", "size": len(src),
              "sha256": hashlib.sha256(src).hexdigest(), "chunk_size": CS})
assert again[1]["reused"] and again[1]["upload_id"] == uid, again
assert call("POST", f"/ingest/{N}/init",
            {"filename": "../evil", "size": 0,
             "sha256": hashlib.sha256(b"").hexdigest(), "chunk_size": CS})[0] == 403
for n in (2, 0, 1):  # out of order, then chunk 1 again to prove idempotence
    assert call("PUT", f"/ingest/{N}/{uid}/chunk/{n}",
                raw=src[n * CS:(n + 1) * CS])[0] == 200
assert call("PUT", f"/ingest/{N}/{uid}/chunk/1", raw=src[CS:2 * CS])[0] == 200
assert call("GET", f"/ingest/{N}/{uid}/status")[1]["stored"] == [0, 1, 2]
code, out = call("POST", f"/ingest/{N}/{uid}/complete")
assert code == 200 and out["ok"], out
assert (sess / "ingest" / "clip.bin").read_bytes() == src
print("  ok   init/chunk/status/complete round trip on the running server")
PYI
check $? "ingest round trip on the live server"
"$PY" -m pytest -q "$SKILL/tests/test_ingest.py" > "$WORK/ingest-pytest.log" 2>&1
check "$?" "chunked ingest pytest suite"
tail -1 "$WORK/ingest-pytest.log" | grep -q "passed" \
  && echo "  ok   pytest suite reported passed" \
  || { echo "  FAIL pytest suite did not report passed"; fails=$((fails+1)); }
kill $WPID 2>/dev/null

echo
if [ "$fails" = "0" ]; then echo "PASS  artifacts in $SESS"; else echo "FAIL  $fails stage(s) - see $WORK"; fi
[ "$fails" = "0" ]
