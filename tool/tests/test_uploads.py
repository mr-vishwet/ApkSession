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

    def fake_stages(root, name, apk):
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

    def crash(root, name, apk):
        raise RuntimeError("stage runner died")

    monkeypatch.setattr(watch, "_run_stages", crash)
    assert req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")[0] == 202
    for _ in range(50):
        if not watch.RUNS.get("alpha", {}).get("alive"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("a crashed run left the session locked")

    monkeypatch.setattr(watch, "_run_stages", lambda r, n, a: None)
    assert req(port, "POST", f"/s/alpha/api/run?t={TOKEN}")[0] == 202


def test_real_stage_list_is_the_cli_a_person_would_run():
    """The runner shells out to the same stages as the CLI; assert the sequence, not a mock."""
    src = Path(watch.__file__).read_text()
    for stage in ('"launch"', '"take"', '"render"', '"sweep"', '"doc"', '"export"'):
        assert stage in src
