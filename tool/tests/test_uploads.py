"""Host-side tests for run inputs: uploading the APK and the brief, deriving cases, and
the run trigger. No device and no adb - the suite boots the real dashboard in-process and
drives the exact HTTP routes the browser hits, so what passes here is what the UI does.

    .venv/bin/python -m pytest tool/tests/test_uploads.py -q
"""
from __future__ import annotations

import hashlib
import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adrd import uploads as UP  # noqa: E402
from adrd import watch  # noqa: E402

TOKEN = "testtok"
APK = b"PK\x03\x04" + b"wae-staging-bytes" * 40
BRIEF = (b"# Find support round\n"
         b"## Step 1 postcode\n"
         b"- [ ] Postcode IG1 1BX reaches Category\n"
         b"1. Empty submit shows required error\n"
         b"* Missing age shows its own error\n")


@pytest.fixture()
def server(tmp_path):
    sessions = tmp_path / "sessions"
    alpha = sessions / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "state.json").write_text(json.dumps({"package": "com.example.app"}))
    cfg = {"dir": sessions, "host": "127.0.0.1", "port": 0, "token": TOKEN,
           "loopback": True, "adb": None, "stream_ttl": 1.0, "allow_unmasked": True}
    srv = watch.Server(cfg)
    srv.serve_thread()
    yield srv.server_address[1], alpha
    srv.shutdown_all()
    watch.RUNS.clear()


def req(port, method, path, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body,
                 headers={"Content-Length": str(len(body))})
    r = conn.getresponse()
    payload = r.read()
    conn.close()
    try:
        return r.status, json.loads(payload)
    except ValueError:
        return r.status, payload


def upload(port, kind, name, body):
    return req(port, "POST", f"/s/alpha/api/upload?t={TOKEN}&kind={kind}&name={name}", body)


# ---- uploads land, and land only inside the session ------------------------------

def test_apk_upload_is_stored_and_listed(server):
    port, alpha = server
    code, out = upload(port, "apk", "WAE_STG_30OCT.apk", APK)
    assert code == 200 and out["ok"], out
    stored = alpha / "inputs" / "app.apk"
    assert stored.read_bytes() == APK
    assert out["sha256"] == hashlib.sha256(APK).hexdigest()
    assert out["bytes"] == len(APK)
    code, listing = req(port, "GET", f"/s/alpha/api/inputs?t={TOKEN}")
    assert code == 200 and listing["apk"]["bytes"] == len(APK)
    assert listing["brief"] is None and listing["cases"] is None


def test_traversal_name_cannot_escape_the_session(server):
    port, alpha = server
    code, out = upload(port, "apk", "../../../tmp/evil.apk", APK)
    assert code == 200, out
    inside = list((alpha / "inputs").rglob("*"))
    assert all(p.parent == alpha / "inputs" for p in inside if p.is_file())
    assert not Path("/tmp/evil.apk").exists()


def test_bad_kind_and_wrong_extension_are_refused(server):
    port, alpha = server
    assert upload(port, "sideload", "a.apk", APK)[0] == 400
    assert upload(port, "", "a.apk", APK)[0] == 400
    assert upload(port, "apk", "payload.exe", APK)[0] == 400
    assert upload(port, "brief", "notes.jar", APK)[0] == 400
    assert not (alpha / "inputs").exists()


def test_oversize_is_refused_before_the_body_is_read(server):
    """The declared length is checked first, so a hostile client cannot make us buffer it."""
    port, alpha = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", f"/s/alpha/api/upload?t={TOKEN}&kind=apk&name=huge.apk")
    conn.putheader("Content-Length", str(UP.MAX_APK + 1))
    conn.endheaders()
    conn.sock.shutdown(http.client.socket.SHUT_WR)  # send nothing; let the server answer
    resp = conn.getresponse()
    code, body = resp.status, resp.read()
    conn.close()
    assert code == 413
    assert "larger than" in body.decode()
    assert not (alpha / "inputs" / "app.apk").exists()


def test_truncated_body_leaves_the_previous_upload_intact(server):
    port, alpha = server
    upload(port, "apk", "first.apk", APK)
    good = (alpha / "inputs" / "app.apk").read_bytes()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", f"/s/alpha/api/upload?t={TOKEN}&kind=apk&name=x.apk")
    conn.putheader("Content-Length", "50000")
    conn.endheaders()
    conn.send(b"only a fragment")
    conn.sock.shutdown(http.client.socket.SHUT_WR)
    status = conn.getresponse().status
    conn.close()
    assert status == 400
    assert (alpha / "inputs" / "app.apk").read_bytes() == good
    assert not list((alpha / "inputs").glob("*part*"))


def test_routes_require_the_token(server):
    port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/s/alpha/api/upload?kind=apk&name=a.apk", body=APK,
                 headers={"Content-Length": str(len(APK))})
    assert conn.getresponse().status == 401
    conn.close()
    assert req(port, "GET", "/s/alpha/api/inputs")[0] == 401


# ---- cases: from the brief when there is one, from the flow map when there is not --

def test_a_system_dialog_that_survives_a_step_is_reported(tmp_path, capsys):
    """An automated round on a struggling emulator recorded "System UI isn't
    responding" over the flow. Every tap after the dialog went nowhere, the walk still
    reported a clean pass, and the video came out showing a stalled screen under mask
    boxes placed for other screens. A single consent prompt is not that - the walk
    answers those on purpose - so only a dialog that survives to the next step counts."""
    from PIL import Image

    from adrd import timeline as T
    from adrd.session import Session

    root = tmp_path / "s1"
    root.mkdir()
    (root / "state.json").write_text(json.dumps({"app": "demo"}))
    s = Session(root)
    walk = {"package": "com.demo.app", "steps": [
        {"label": "open", "screen": "home", "chapter": "c1"},
        {"label": "fill", "screen": "form", "chapter": "c1"},
        {"label": "save", "screen": "form", "chapter": "c1"}]}
    DIALOG = "com.android.systemui/.anr.AnrDialog"

    class Stuck:
        def screen_size(self):
            return (1080, 2400)

        def screencap(self, dest):
            Stuck.n += 1
            Image.new("RGB", (60, 100), (Stuck.n % 4, 0, 0)).save(dest)
            return dest

        def current_focus(self):
            return DIALOG

    Stuck.n = 0
    events = T.run(Stuck(), s, walk, settle=0)
    assert "stuck" not in events[0], "the first dialog is only a prompt"
    assert [e.get("stuck") for e in events[1:]] == [DIALOG, DIALOG]
    assert "STALLED: 2 of 3 steps" in capsys.readouterr().out

    class Answered(Stuck):
        """A consent prompt the walk dismisses: dialog, then the app, then the app."""
        seen = 0

        def current_focus(self):
            Answered.seen += 1
            return DIALOG if Answered.seen == 1 else "com.demo.app/.MainActivity"

    events = T.run(Answered(), s, walk, settle=0)
    assert all("stuck" not in e for e in events)
    assert "STALLED" not in capsys.readouterr().out


def test_a_step_that_misses_its_calibrated_screen_is_counted(tmp_path, capsys):
    """The scout pass is the calibration: it records what each step is supposed to land
    on, and the take compares. Without that write-back a walk that never left the
    postcode form reported fourteen clean steps."""
    from PIL import Image

    from adrd import timeline as T
    from adrd.img import ahash
    from adrd.session import Session

    root = tmp_path / "s2"
    (root / "stills").mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({"app": "demo"}))
    s = Session(root)

    def pattern(seed):
        im = Image.new("L", (64, 64))
        for x in range(64):
            for y in range(64):
                im.putpixel((x, y), 255 if (x // 8 + y // 8 + seed) % 2 else 0)
        return im

    pattern(0).save(root / "stills" / "screen_a.png")
    pattern(3).save(root / "stills" / "screen_b.png")
    a = hex(ahash(root / "stills" / "screen_a.png"))
    b = hex(ahash(root / "stills" / "screen_b.png"))
    assert a != b, "the two calibration screens must be distinguishable"

    class Device:
        def screen_size(self):
            return (1080, 2400)

        def screencap(self, dest):
            pattern(0).save(dest)
            return dest

        def current_focus(self):
            return "com.demo.app/.MainActivity"

    walk = {"package": "com.demo.app", "steps": [
        {"label": f"s{i}", "screen": "form", "chapter": "c1", "verify": True}
        for i in range(3)]}
    for st in walk["steps"]:
        st["expect_hash"] = a
    events = T.run(Device(), s, walk, settle=0)
    assert not any(e.get("mismatch") for e in events)
    assert "CALIBRATION" not in capsys.readouterr().out

    for st in walk["steps"]:
        st["expect_hash"] = b
    events = T.run(Device(), s, walk, settle=0)
    assert [e.get("mismatch") for e in events] == [True] * 3
    out = capsys.readouterr().out
    assert "CALIBRATION: 3 of 3 steps" in out and "2 of those are consecutive" in out


def test_the_runs_own_records_are_never_mistaken_for_the_brief(server):
    """launch/take/render write run.log and run.json into inputs/, next to the brief.
    They sort after every real filename, so after one run the page named the log as the
    instructions and Build cases would have parsed stage output for test steps."""
    port, alpha = server
    upload(port, "apk", "app.apk", APK)
    upload(port, "brief", "find-support-cases.md", BRIEF)
    (alpha / "inputs" / "run.log").write_text("== launch ==\nexit=0\n== take ==\nexit=0\n")
    (alpha / "inputs" / "run.json").write_text(json.dumps({"stages": []}))

    code, out = req(port, "GET", f"/s/alpha/api/inputs?t={TOKEN}")
    assert code == 200 and out["brief"]["name"] == "find-support-cases.md", out

    code, cases = req(port, "POST", f"/s/alpha/api/cases?t={TOKEN}")
    assert code == 200 and cases["origin"] == "brief" and cases["count"] == 3, cases


def test_brief_produces_numbered_cases(server):
    port, alpha = server
    upload(port, "apk", "app.apk", APK)
    code, out = upload(port, "brief", "find-support-cases.md", BRIEF)
    assert code == 200, out
    code, cases = req(port, "POST", f"/s/alpha/api/cases?t={TOKEN}")
    assert code == 200 and cases["origin"] == "brief" and cases["count"] == 3
    doc = (alpha / "CASES.md").read_text()
    for title in ("Postcode IG1 1BX reaches Category", "Empty submit shows required error",
                  "Missing age shows its own error"):
        assert title in doc
    parsed = json.loads((alpha / "cases.json").read_text())
    assert [c["id"] for c in parsed["cases"]] == ["C01", "C02", "C03"]
    assert all(c["section"] == "Step 1 postcode" for c in parsed["cases"])


def test_no_brief_falls_back_to_the_flow_inventory(server):
    port, alpha = server
    (alpha / "flow_map.json").write_text(json.dumps(
        {"routes": [{"name": "find_support", "module": "flow"},
                    {"name": "profile", "module": "protected"}]}))
    code, cases = req(port, "POST", f"/s/alpha/api/cases?t={TOKEN}")
    assert code == 200 and cases["origin"] == "flow" and cases["count"] == 2
    assert "Reach find_support" in (alpha / "CASES.md").read_text()


def test_pdf_brief_reports_the_missing_dependency_instead_of_inventing_text(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    (root / "inputs").mkdir()
    (root / "inputs" / "brief.pdf").write_bytes(b"%PDF-1.4 not really")
    try:
        import pypdf  # noqa: F401
        pytest.skip("pypdf installed, PDF briefs are parsed for real")
    except ImportError:
        pass
    with pytest.raises(UP.UploadError) as e:
        UP.build_cases(root)
    assert "pypdf" in str(e.value)


# ---- the run trigger -------------------------------------------------------------

def test_run_needs_an_apk_first(server):
    port, _ = server
    code, out = req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")
    assert code == 400 and "apk" in str(out["error"])


def test_run_starts_and_refuses_a_second_one(server, monkeypatch):
    port, alpha = server
    release = threading.Event()
    started = threading.Event()

    def fake_stages(root, name, apk, raw=False):
        started.set()
        release.wait(10)

    monkeypatch.setattr(watch, "_run_stages", fake_stages)
    upload(port, "apk", "app.apk", APK)
    code, out = req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")
    assert code == 202 and out["ok"]
    assert started.wait(5)
    code, second = req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")
    assert code == 409 and "already" in str(second["error"])
    _, listing = req(port, "GET", f"/s/alpha/api/inputs?t={TOKEN}")
    assert listing["running"] is True
    release.set()
    for _ in range(50):
        _, listing = req(port, "GET", f"/s/alpha/api/inputs?t={TOKEN}")
        if not listing["running"]:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the run flag never cleared")


def test_a_crashing_stage_runner_frees_the_session_for_the_next_run(server, monkeypatch):
    """The bug this guards: a stage that raised left the session marked running forever,
    so every later run got 409 until the dashboard was restarted."""
    port, alpha = server
    upload(port, "apk", "app.apk", APK)

    def crash(root, name, apk, raw=False):
        raise RuntimeError("stage runner died")

    monkeypatch.setattr(watch, "_run_stages", crash)
    assert req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")[0] == 202
    for _ in range(50):
        if not watch.RUNS.get("alpha", {}).get("alive"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("a crashed run left the session locked")

    monkeypatch.setattr(watch, "_run_stages", lambda *a, **k: None)
    assert req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")[0] == 202


def test_export_excludes_the_unmasked_capture_unless_the_run_asks(server, monkeypatch):
    """The bundle is what leaves the machine. Shipping unmasked client screens by default
    is the leak this guards."""
    import subprocess

    port, alpha = server
    upload(port, "apk", "app.apk", APK)

    def record(cmd, *a, **k):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    recorded = []
    monkeypatch.setattr(watch.subprocess, "run", record)
    req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")
    for _ in range(80):
        if not watch.RUNS.get("alpha"):
            break
        time.sleep(0.1)
    exports = [c for c in recorded if "export" in c]
    assert exports, recorded
    assert "--no-raw" in exports[0] and "--yes" not in exports[0]

    recorded.clear()
    req(port, "POST", f"/s/alpha/api/run?t={TOKEN}&raw=1")
    for _ in range(80):
        if not watch.RUNS.get("alpha"):
            break
        time.sleep(0.1)
    exports = [c for c in recorded if "export" in c]
    assert exports and "--yes" in exports[0] and "--no-raw" not in exports[0]


def test_a_tunnel_pointed_at_another_port_is_not_reused(tmp_path, monkeypatch):
    """Reusing a recorded tunnel whose origin is a different local port prints a URL
    that 502s - the exact failure that breaks "open this anywhere" promises."""
    d = tmp_path / "sessions"
    d.mkdir()
    watch.tunnel_record(d).write_text(json.dumps(
        {"url": "https://x.trycloudflare.com", "pid": 999999,
         "local": "http://127.0.0.1:8734"}))
    monkeypatch.setattr(watch, "_alive", lambda pid: True)
    assert watch.tunnel_status(d) == "https://x.trycloudflare.com"
    assert watch.tunnel_status(d, 8734) == "https://x.trycloudflare.com"
    assert watch.tunnel_status(d, 8735) is None


def test_mask_picker_box_lands_in_branding_and_rejects_bad_geometry(server, monkeypatch):
    """The picker exists because typed pixel rects were guessed three times and each
    landed on the wrong band - so the box must come from the frame under review."""
    port, alpha = server
    (alpha / "timeline.json").write_text(json.dumps(
        {"size": [1080, 2400], "events": [
            {"still": str(alpha / "stills" / "x1.png"), "screen": "find_support",
             "label": "step 1"}]}))
    (alpha / "stills").mkdir()
    (alpha / "stills" / "x1.png").write_bytes(b"not really a png, size is not read")

    code, listing = req(port, "GET", f"/s/alpha/api/picklist?t={TOKEN}")
    assert code == 200 and listing["size"] == [1080, 2400]
    assert listing["stills"][0]["screen"] == "find_support"

    bad = [
        f"/s/alpha/api/box?t={TOKEN}&screen=find_support&mode=solid",
        f"/s/alpha/api/box?t={TOKEN}&screen=find_support&mode=shrink&x=1&y=1&w=50&h=50",
        f"/s/alpha/api/box?t={TOKEN}&screen=../../etc&x=1&y=1&w=50&h=50&mode=solid",
        f"/s/alpha/api/box?t={TOKEN}&screen=find_support&x=900&y=100&w=500&h=500&mode=solid",
        f"/s/alpha/api/box?t={TOKEN}&screen=find_support&x=0&y=0&w=2&h=2&mode=solid",
    ]
    for path in bad:
        code, out = req(port, "POST", path)
        assert code == 400, (path, out)

    code, out = req(port, "POST",
                    f"/s/alpha/api/box?t={TOKEN}&screen=find_support&x=290&y=160&w=520&h=110"
                    f"&label=app%20bar%20wordmark&mode=solid")
    assert code == 200, out
    entries = json.loads((alpha / "branding.json").read_text())["entries"]
    box = [e for e in entries if e["label"] == "app bar wordmark"]
    assert box and box[0]["rect"] == {"x": 290, "y": 160, "w": 520, "h": 110}
    assert box[0]["scope"] == "screen:find_support" and box[0]["mode"] == "solid"


def test_the_report_renders_and_says_so_when_the_tool_is_absent(tmp_path, monkeypatch):
    """A run used to stop at markdown. The reviewer wants one file to hand over, so the
    same text has to lay out as a report - and a machine without the converter still has
    to finish the run with a message instead of a traceback."""
    from adrd import docgen

    md = ("# Walkthrough - app\n\n- Recorded: today\n\n**6 sample(s) show a box**\n\n"
          "| # | Module |\n|---|---|\n| 1 | login <a> |\n")
    page = docgen.html(md, "report")
    assert "<h1>Walkthrough - app</h1>" in page
    assert "<th>Module</th>" in page and "<td>login &lt;a&gt;</td>" in page
    assert "<b>6 sample(s) show a box</b>" in page
    assert "<li>Recorded: today</li>" in page

    monkeypatch.setattr(docgen.shutil, "which", lambda name: None)
    with pytest.raises(docgen.MissingTool):
        docgen.pdf(md, "report", tmp_path / "SESSION_DOC.pdf")


def test_a_box_drawn_on_the_page_can_be_removed_from_the_page(server):
    """The picker could only ever add. Undoing a box that landed on the wrong band meant
    editing branding.json by hand while the run was reading it, so every box is listed
    with a remove button and the endpoint honours it."""
    port, alpha = server
    (alpha / "timeline.json").write_text(json.dumps({"size": [1080, 2400], "events": []}))
    code, out = req(port, "POST",
                    f"/s/alpha/api/box?t={TOKEN}&screen=home&x=290&y=160&w=520&h=110"
                    f"&label=wordmark&mode=solid")
    assert code == 200, out

    code, st = req(port, "GET", f"/s/alpha/state?t={TOKEN}")
    assert code == 200, st
    box = st["boxes"][0]
    assert box["i"] == 0 and box["scope"] == "screen:home" and box["label"] == "wordmark"
    assert [round(v, 3) for v in box["frac"]] == [0.269, 0.067, 0.481, 0.046]

    code, out = req(port, "POST", f"/s/alpha/api/box?t={TOKEN}&op=rm&i=0")
    assert code == 200 and out["left"] == 0, out
    assert json.loads((alpha / "branding.json").read_text())["entries"] == []

    for bad in ("i=0", "i=abc", "i=7", "i=-1"):
        code, out = req(port, "POST", f"/s/alpha/api/box?t={TOKEN}&op=rm&{bad}")
        assert code == 400, (bad, out)


def test_the_run_records_every_stage_so_the_page_can_show_the_pipeline(tmp_path, monkeypatch):
    """Someone watching the demo needs to see launch -> take -> brand -> render -> doc
    -> export move along, not scroll a log. A stage that fails must stay visible as the
    reason the run stopped, with the stages after it still waiting."""
    root = tmp_path / "sessions" / "beta"
    (root / "inputs").mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({"app": "beta", "package": "com.x.app"}))
    apk = root / "inputs" / "app.apk"
    apk.write_bytes(APK)
    def fake(cmd, **kw):
        stage = cmd[2]                      # [python, adrd.py, <stage>, --session, ...]
        code = 1 if stage == "sweep" else 0
        return type("R", (), {"returncode": code, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(watch.subprocess, "run", fake)
    watch._run_stages(root, "beta", apk)

    run = json.loads((root / "inputs" / "run.json").read_text())
    names = [h["name"] for h in run["stages"]]
    assert names == ["launch", "take", "brand", "render", "sweep", "doc", "export"], names
    states = {h["name"]: h["state"] for h in run["stages"]}
    assert states["launch"] == "ok" and states["render"] == "ok"
    assert states["sweep"] == "failed", states
    assert states["doc"] == "waiting" and states["export"] == "waiting"
    assert run["raw"] is False and run["started"]

    view = watch.session_view(root)
    assert [h["state"] for h in view["run"]["stages"]] == list(states.values())


def test_a_stage_left_running_by_a_dead_runner_is_called_interrupted(tmp_path):
    root = tmp_path / "sessions" / "gamma"
    (root / "inputs").mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({"app": "gamma"}))
    (root / "inputs" / "run.json").write_text(json.dumps(
        {"started": "x", "raw": False,
         "stages": [{"name": "take", "state": "running", "seconds": None},
                    {"name": "render", "state": "waiting", "seconds": None}]}))
    stages = watch.session_view(root)["run"]["stages"]
    assert stages[0]["state"] == "interrupted"
    assert stages[1]["state"] == "waiting"


def test_a_box_can_be_moved_onto_another_screens_time_window(server):
    """Every leak that survived a sweep last round was a box drawn on the right pixels
    but filed under a screen whose window did not cover the seconds it needed. The page
    has to let that one field change without redrawing the box."""
    port, alpha = server
    (alpha / "timeline.json").write_text(json.dumps(
        {"size": [1080, 2400], "events": [],
         "screens": {"home": [[0.0, 12.0]], "category": [[90.0, 140.0]]}}))
    code, out = req(port, "POST",
                    f"/s/alpha/api/box?t={TOKEN}&screen=home&x=290&y=160&w=520&h=110"
                    f"&label=logo%20tiles&mode=solid")
    assert code == 200, out

    code, st = req(port, "GET", f"/s/alpha/state?t={TOKEN}")
    assert st["screens"]["category"] == [[90.0, 140.0]]
    assert st["boxes"][0]["scope"] == "screen:home"

    code, out = req(port, "POST", f"/s/alpha/api/box?t={TOKEN}&op=set&i=0&screen=category")
    assert code == 200 and out["scope"] == "category", out
    entries = json.loads((alpha / "branding.json").read_text())["entries"]
    assert entries[0]["scope"] == "screen:category"
    assert entries[0]["rect"] == {"x": 290, "y": 160, "w": 520, "h": 110}, "geometry kept"

    code, out = req(port, "POST", f"/s/alpha/api/box?t={TOKEN}&op=set&i=0&screen=global")
    assert code == 200, out
    assert json.loads((alpha / "branding.json").read_text())["entries"][0]["scope"] == "global"

    for bad in ("screen=../../etc", "screen=", "i=9&screen=home", "i=x&screen=home"):
        code, out = req(port, "POST", f"/s/alpha/api/box?t={TOKEN}&op=set&{bad}")
        assert code == 400, (bad, out)


def test_a_preset_repeated_does_not_stack_boxes():
    """The run stage calls `brand preset` every time, so a second pass used to append a
    second copy of all five boxes over the same pixels - the render still looked masked
    while telling nobody that the layer count had doubled."""
    from adrd import render as R

    first = R.preset_entries(["debug-banner", "topbar-title"], ["home", "find_support"],
                             (1080, 2400))
    assert len(first) == 1 + 2
    second = R.preset_entries(["debug-banner", "topbar-title"], ["home", "find_support"],
                              (1080, 2400), first)
    assert second == []
    # a screen the first pass never saw still gets its box
    third = R.preset_entries(["topbar-title"], ["home", "needs"], (1080, 2400), first)
    assert [e["scope"] for e in third] == ["screen:needs"]


def test_server_is_threaded_so_the_live_view_cannot_wedge_it():
    """The dashboard holds /stream open for as long as a tab is looking at it. On a plain
    TCPServer that single handler blocked every other request and the page looked blank."""
    import socketserver

    assert issubclass(watch.Server, socketserver.ThreadingMixIn)
    assert watch.Server.daemon_threads is True
    assert watch.Server.block_on_close is False, "shutdown would wait on open streams"


def test_requests_still_answer_while_a_stream_is_open(server):
    import socket

    port, _ = server
    held = socket.create_connection(("127.0.0.1", port), timeout=5)
    held.sendall(f"GET /s/alpha/stream?t={TOKEN} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    held.recv(4096)  # the stream keeps this connection alive
    try:
        assert req(port, "GET", f"/s/alpha/state?t={TOKEN}")[0] == 200
        assert req(port, "GET", f"/s/alpha/api/inputs?t={TOKEN}")[0] == 200
    finally:
        held.close()


def test_caption_with_an_apostrophe_cannot_break_the_filtergraph(tmp_path):
    """A walk label like "open Let's Find Support" used to close the drawtext option
    early, because ffmpeg has no escape for a quote inside single quotes. The commas of
    the next enable='gte(t,..)*lt(t,..)' then read as filter separators and render died."""
    from adrd import ff

    caps = [{"text": "C00c open Let's Find Support", "enable": "gte(t,1.000)*lt(t,2.000)",
             "fontsize": 40, "y": 100}]
    graph, _ = ff.build_graph((1080, 2400), [], caps, "bottom", None,
                              textdir=tmp_path / "captions")
    assert "textfile=" in graph
    assert "text=\"C00c" not in graph and "Let\\'s" not in graph
    assert (tmp_path / "captions" / "cap00.txt").read_text() == "C00c open Let's Find Support"
    assert "enable='gte(t,1.000)*lt(t,2.000)'" in graph


def test_named_tunnel_config_routes_the_fixed_host(tmp_path):
    """`--url` only exists for quick tunnels, so a named tunnel needs an ingress file -
    and cloudflared rejects a config without a catch-all rule."""
    p = watch.tunnel_config(tmp_path, "live", "abc-123", "live.example.com",
                            "http://127.0.0.1:8734")
    txt = p.read_text()
    assert "tunnel: abc-123" in txt
    assert "hostname: live.example.com" in txt
    assert "service: http://127.0.0.1:8734" in txt
    assert "http_status:404" in txt
    assert str(Path.home() / ".cloudflared" / "live.json") in txt


def test_hostname_without_cloudflare_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(watch.shutil, "which", lambda name: None)
    assert watch.tunnel("http://127.0.0.1:8734", tmp_path / "t.log", tmp_path,
                        hostname="live.example.com") is None


def test_step_captions_get_their_own_slice_when_the_clock_collapses():
    """screenrecord drops idle frames, so a paused walkthrough can put every step on the
    same video instant and all the captions burn in together."""
    from adrd.render import step_windows

    collapsed = [23.993] * 5
    wins = step_windows(collapsed, 20.0, 50.0, 120.0)
    assert len(wins) == 5
    starts = [s for s, _ in wins]
    assert starts == sorted(starts) and len(set(starts)) == 5, starts
    assert all(e - s >= 0.6 for s, e in wins)
    assert wins[0][0] >= 20.0 and wins[-1][1] <= 50.0 + 0.001

    distinct = step_windows([5.0, 12.0, 30.0], 0.0, 40.0, 60.0)
    assert distinct == [(5.0, 12.0), (12.0, 30.0), (30.0, 40.0)]

    assert step_windows([], 0.0, 10.0, 10.0) == []

    # steps recorded out of order still play in walk order, never overlapping
    messy = step_windows([10.0, 10.0, 4.0], 0.0, 30.0, 30.0)
    assert [s for s, _ in messy] == sorted(s for s, _ in messy)
    assert all(messy[i][1] <= messy[i + 1][1] for i in range(len(messy) - 1))


def test_real_stage_list_is_the_cli_a_person_would_run():
    """The runner shells out to the same stages as the CLI; assert the sequence, not a mock."""
    src = Path(watch.__file__).read_text()
    for stage in ('"launch"', '"take"', '"brand"', '"render"', '"sweep"', '"doc"', '"export"'):
        assert stage in src
    assert src.index('"brand"') < src.index('"render"'), "render needs branding.json first"
    # `brand preset` with no --name only lists the presets and exits 2
    assert '"--name", "debug-banner", "topbar-title", "status-bar"' in src
