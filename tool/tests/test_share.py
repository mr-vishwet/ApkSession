"""Host-side tests for live links: who may read a session, what a recipient can download,
and the trigger trail when a run starts or finishes. No device, no SMTP - the outbox is
the source of truth when delivery is not configured.

    .venv/bin/python -m pytest tool/tests/test_share.py -q
"""
from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adrd import share as SH  # noqa: E402
from adrd import watch  # noqa: E402

TOKEN = "testtok"


@pytest.fixture()
def server(tmp_path):
    sessions = tmp_path / "sessions"
    alpha = sessions / "alpha"
    (alpha / "render" / "sections").mkdir(parents=True)
    (alpha / "stills").mkdir()
    (alpha / "state.json").write_text(json.dumps(
        {"app": "Example", "package": "com.example.app"}))
    (alpha / "render" / "demo_debranded.mp4").write_bytes(b"masked-video" * 20)
    (alpha / "render" / "SESSION_DOC.md").write_text("# report\n")
    (alpha / "render" / "sections" / "01-splash.mp4").write_bytes(b"clip" * 10)
    (alpha / "render" / "sections" / "01-splash.jpg").write_bytes(b"jpeg" * 10)
    (alpha / "recording.mp4").write_bytes(b"raw" * 10)
    (alpha / "timeline.json").write_text(json.dumps({"chapters": []}))
    (alpha / "stills" / "000.png").write_bytes(b"png")
    cfg = {"dir": sessions, "host": "127.0.0.1", "port": 0, "token": TOKEN,
           "loopback": True, "adb": None, "stream_ttl": 1.0, "allow_unmasked": True}
    srv = watch.Server(cfg)
    srv.serve_thread()
    yield srv.server_address[1], alpha
    srv.shutdown_all()
    watch.RUNS.clear()


def req(port, method, path, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers={"Content-Length": str(len(body))})
    r = conn.getresponse()
    payload = r.read()
    conn.close()
    try:
        return r.status, json.loads(payload)
    except ValueError:
        return r.status, payload


def make_link(port):
    code, out = req(port, "POST", f"/s/alpha/api/share?t={TOKEN}")
    assert code == 200 and out["link"], out
    return out["link"].split("t=", 1)[1]


def test_only_the_masked_files_are_shareable(tmp_path):
    assert SH.shareable("render/demo_debranded.mp4")
    assert SH.shareable("render/demo_debranded.vtt")
    assert SH.shareable("render/sections/01-splash.mp4")
    assert SH.shareable("render/sections/01-splash.jpg")
    assert SH.shareable("render/SESSION_DOC.pdf")
    assert not SH.shareable("recording.mp4")
    assert not SH.shareable("timeline.json")
    assert not SH.shareable("stills/000.png")
    assert not SH.shareable("render/sections/../../recording.mp4")


def test_subscribe_validates_and_dedupes(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    with pytest.raises(ValueError):
        SH.subscribe(root, "not-an-email")
    assert SH.subscribe(root, "A@B.co")["subscribers"] == 1
    assert SH.subscribe(root, "a@b.co")["subscribers"] == 1
    assert SH.subscribers(root) == ["a@b.co"]


def test_no_smtp_still_writes_the_outbox(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    SH.subscribe(root, "a@b.co")
    assert not SH.smtp_configured()
    out = SH.notify(root, "started", base="http://example.test", blocking=True)
    assert out["queued"] == 1 and out["sent"] == 0
    files = list((root / "share" / "outbox").glob("*.json"))
    assert len(files) == 1
    queued = json.loads(files[0].read_text())
    assert queued["to"] == "a@b.co" and queued["status"].startswith("queued")
    assert "http://example.test/live/" in queued["body"]


def test_link_trigger_reaches_only_the_asker(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    SH.subscribe(root, "one@b.co")
    SH.subscribe(root, "two@b.co")
    out = SH.notify(root, "link", base="http://x", blocking=True, only="two@b.co")
    assert out["subscribers"] == 1
    files = list((root / "share" / "outbox").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["to"] == "two@b.co"


def test_start_and_finish_leave_one_event_each(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    SH.subscribe(root, "a@b.co")
    SH.notify(root, "started", base="http://x", blocking=True)
    SH.notify(root, "completed", base="http://x", blocking=True, kind="render")
    events = SH.recent_events(root)
    assert [e["event"] for e in events] == ["started", "render"]
    assert all(e["subject"] for e in events)


def test_live_link_needs_its_own_token(server):
    port, _ = server
    assert req(port, "GET", "/live/alpha/")[0] == 401
    assert req(port, "GET", "/live/alpha/?t=wrong")[0] == 401
    tok = make_link(port)
    code, body = req(port, "GET", f"/live/alpha/?t={tok}")
    assert code == 200
    text = body.decode()
    assert "Section cuts" in text and "Mask picker" not in text
    assert "/s/alpha" not in text  # no path back to the operator console


def test_live_state_has_no_operator_internals(server):
    port, _ = server
    tok = make_link(port)
    code, out = req(port, "GET", f"/live/alpha/state?t={tok}")
    assert code == 200
    for hidden in ("boxes", "walk", "screens", "device"):
        assert hidden not in out, hidden
    assert "sections" in out and "events" in out


def test_live_downloads_stop_at_the_masked_files(server):
    port, alpha = server
    tok = make_link(port)
    assert req(port, "GET", f"/live/alpha/f/render/demo_debranded.mp4?t={tok}")[0] == 200
    assert req(port, "GET", f"/live/alpha/f/render/sections/01-splash.jpg?t={tok}")[0] == 200
    assert req(port, "GET", f"/live/alpha/f/recording.mp4?t={tok}")[0] == 403
    assert req(port, "GET", f"/live/alpha/f/timeline.json?t={tok}")[0] == 403
    assert req(port, "GET", f"/live/alpha/f/stills/000.png?t={tok}")[0] == 403


def test_subscribe_through_the_live_route(server):
    port, alpha = server
    tok = make_link(port)
    body = json.dumps({"email": "viewer@example.com"}).encode()
    assert req(port, "POST", f"/live/alpha/subscribe?t={tok}", body)[0] == 200
    assert SH.subscribers(alpha) == ["viewer@example.com"]
    assert req(port, "POST", "/live/alpha/subscribe", body)[0] == 401


def test_operator_email_queues_the_link(server):
    port, alpha = server
    code, out = req(port, "POST",
                    f"/s/alpha/api/share?t={TOKEN}&email=viewer@example.com")
    assert code == 200 and out["ok"], out
    assert SH.subscribers(alpha) == ["viewer@example.com"]
    assert list((alpha / "share" / "outbox").glob("*.json"))
    events = SH.recent_events(alpha)
    assert events[-1]["event"] == "link"


def test_a_crashed_run_still_releases_the_session(tmp_path, monkeypatch):
    root = tmp_path / "sessions" / "alpha"
    root.mkdir(parents=True)
    seen = []

    def boom(*a, **k):
        raise RuntimeError("adb vanished")

    monkeypatch.setattr(watch, "_run_stages", boom)
    monkeypatch.setattr(watch.SH, "notify",
                        lambda r, e, **k: seen.append(e) or {"event": e})
    watch.RUNS["alpha"] = {"alive": True}
    watch._run_guarded(root, "alpha", root / "inputs" / "app.apk")
    assert "alpha" not in watch.RUNS
    assert seen == ["failed"]
