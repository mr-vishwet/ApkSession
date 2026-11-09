"""Local dashboard: what the recorder is seeing right now, and where the task stands.

Stdlib only. Binds to 127.0.0.1 unless told otherwise, and requires a token when it is
not loopback, because the live frames are unmasked client app content.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import ff
from . import ingest as IN
from . import share as SH
from . import uploads as UP
from .env import cf_cert, cf_dir
from .session import Session

BOUNDARY = "adrdframe"
RAW_MARK = "recording"
ARTIFACTS = [("final", "shareable video"), ("sections", "section clips"),
             ("vtt", "chapters"), ("doc", "session doc"), ("report", "A4 report"),
             ("recording", "raw capture (unmasked)"), ("sheet", "screen sheet"),
             ("proof", "region proof"), ("bundle", "final export bundle")]
RUN_STAGES = ("launch", "take", "brand", "retimeline", "render", "sections", "sweep",
            "doc", "export")
LIVE_TTL = 45.0  # a status file this old means nothing is running

RUN_LOCK = threading.Lock()
RUNS: dict[str, dict] = {}

_ADRD_PATH: str | None = None


def _adrd_path() -> str:
    global _ADRD_PATH
    if _ADRD_PATH is None:
        _ADRD_PATH = str(Path(__file__).resolve().parent.parent / "adrd.py")
    return _ADRD_PATH


def _run_guarded(root: Path, name: str, apk: Path, raw: bool = False,
                 only: tuple[str, ...] | None = None, base: str = "") -> None:
    """Whatever happens to the stages, the session must stop being marked as running -
    otherwise one failed launch locks that session out of every later run. The same
    moment is the only honest place to send the finished (or failed) trigger."""
    ok = False
    try:
        ok = _run_stages(root, name, apk, raw, only) is not False
    except Exception as exc:
        # this runs in a thread, so an uncaught error would die there with no record
        # the console can show; leave the reason where the run log lives
        try:
            with (root / "inputs" / "run.log").open("a", encoding="utf-8") as lf:
                lf.write(f"== runner ==\nrunner crashed: {exc!r}\n")
            write_status(root, stage=f"runner: {exc}")
        except Exception:
            pass
    finally:
        with RUN_LOCK:
            RUNS.pop(name, None)
        try:
            SH.notify(root, "completed" if ok else "failed", base=base,
                      kind="render" if only is not None else "run")
        except Exception:
            pass


def _run_stages(root: Path, name: str, apk: Path, raw: bool = False,
                only: tuple[str, ...] | None = None) -> bool:
    """Run launch -> take -> render -> sweep -> doc -> export, one stage at a
    time, writing each stage to status.json and appending output to
    inputs/run.log so the dashboard can show progress. `only` narrows the plan for a
    re-render after a box edit, which must not reinstall the app or re-record."""
    log = root / "inputs" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    adrd = _adrd_path()
    stages = [
        ("launch", [py, adrd, "launch", "--session", str(root), "--apk", str(apk), "--clear"]),
        ("take", [py, adrd, "take", "--session", str(root)]),
        ("brand", [py, adrd, "brand", "preset", "--session", str(root),
                   "--name", "debug-banner", "topbar-title", "status-bar"]),
        # rebuilds the chapter and screen windows from the recorded events, so a walk
        # re-tag made after the take is honoured by the very next render
        ("retimeline", [py, adrd, "retimeline", "--session", str(root)]),
        ("render", [py, adrd, "render", "--session", str(root), "--step-captions"]),
        ("sections", [py, adrd, "sections", "--session", str(root)]),
        ("sweep", [py, adrd, "sweep", "--session", str(root)]),
        ("doc", [py, adrd, "doc", "--session", str(root)]),
        # the bundle excludes the unmasked capture unless the caller asked for it by name
        ("export", [py, adrd, "export", "--session", str(root),
                    "--yes" if raw else "--no-raw"]),
    ]
    if only is None:
        only = RUN_STAGES
    if only:
        stages = [s for s in stages if s[0] in only]
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    hist = [{"name": n, "state": "waiting", "seconds": None} for n, _ in stages]
    rec = root / "inputs" / "run.json"

    def note(name: str, state: str, seconds: float | None = None) -> None:
        """The page shows the whole pipeline, not just the current line of the log, so a
        viewer can see how far the run got when a stage dies on a device limit."""
        for h in hist:
            if h["name"] == name:
                h.update(state=state,
                         seconds=seconds if seconds is not None else h["seconds"])
        rec.write_text(json.dumps({"started": started, "raw": raw, "stages": hist,
                                   "at": round(time.time(), 2)}))

    for stage, cmd in stages:
        note(stage, "running")
        write_status(root, stage=stage)
        with log.open("a", encoding="utf-8") as lf:
            lf.write(f"== {stage} ==\n")
            lf.flush()
        t0 = time.time()
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except Exception as exc:
            with log.open("a", encoding="utf-8") as lf:
                lf.write(f"{stage} raised: {exc}\n")
            write_status(root, stage=f"{stage}: error")
            note(stage, "failed", round(time.time() - t0, 1))
            return False
        with log.open("a", encoding="utf-8") as lf:
            lf.write(cp.stdout)
            lf.write(cp.stderr)
            lf.write(f"exit={cp.returncode}\n")
        secs = round(time.time() - t0, 1)
        if cp.returncode != 0:
            write_status(root, stage=f"{stage}: failed (exit {cp.returncode})")
            note(stage, "failed", secs)
            return False
        note(stage, "ok", secs)
    write_status(root, stage="done")
    return True


class Frames:
    """One shared screencap loop; N viewers never means N captures."""

    def __init__(self, adb=None, interval: float = 0.6):
        self.adb, self.interval = adb, interval
        self.jpeg: bytes | None = None
        self.seq = 0
        self.error: str | None = None
        self.stopped = threading.Event()
        if adb:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stopped.is_set():
            t0 = time.time()
            try:
                cp = subprocess.run(self.adb.argv("exec-out", "screencap", "-p"),
                                    capture_output=True, timeout=20)
                if cp.returncode == 0 and len(cp.stdout) > 2048:
                    self.jpeg, self.error, self.seq = cp.stdout, None, self.seq + 1
                else:
                    self.error = "screencap blocked - FLAG_SECURE screen or OEM capture limit"
            except Exception as exc:
                self.error = f"screencap failed: {exc}"
            time.sleep(max(0.05, self.interval - (time.time() - t0)))

    def stop(self) -> None:
        self.stopped.set()


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
EVIDENCE_DIRS = ("stills", "stills_annotated", "render/proof_frames",
                "render/proof_crops", "render")


def evidence_files(root: Path) -> list[Path]:
    out = []
    for d in EVIDENCE_DIRS:
        base = root / d
        if base.exists():
            out += [f for f in sorted(base.rglob("*"))
                    if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES
                    and f.stat().st_size < 8_000_000]
    return out


def evidence_list(root: Path) -> list[dict]:
    return [{"rel": f.relative_to(root).as_posix(), "bytes": f.stat().st_size}
            for f in evidence_files(root)]


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def parse_progress(path: Path) -> dict:
    """ffmpeg -progress writes key=value lines; out_time is the honest numerator."""
    try:
        lines = dict(l.split("=", 1) for l in path.read_text().splitlines() if "=" in l)
    except Exception:
        return {}
    out = {"raw": lines.get("progress", ""), "out_time": lines.get("out_time", "")}
    try:
        h, m, s = out["out_time"].split(":")
        out["seconds"] = round(int(h) * 3600 + int(m) * 60 + float(s), 2)
    except Exception:
        pass
    return out


def session_view(root: Path) -> dict:
    s = Session(root)
    st = s.state()
    status = read_json(s.root / "status.json", {})
    if time.time() - float(status.get("at", 0) or 0) > LIVE_TTL:
        status = {}
    tl = read_json(s.path("timeline"), {})
    rend = read_json(s.path("render"), {})
    sec = read_json(s.path("sections"), {}) or {}
    branding = read_json(s.path("branding"), {"entries": []})
    boxes = []
    for i, e in enumerate(branding.get("entries", [])):
        frac = e.get("frac") or {}
        boxes.append({"i": i, "scope": e.get("scope", ""), "label": e.get("label", ""),
                      "mode": e.get("mode", "blur"), "preset": e.get("preset") or "",
                      "frac": [frac.get("x", 0), frac.get("y", 0),
                               frac.get("w", 0), frac.get("h", 0)]})
    arts = []
    walk = read_json(s.root / "walk.json", {}) or {}
    steps_w = [{"i": n, "screen": st.get("screen") or "", "label": st.get("label") or "",
                "ops": ",".join(o.get("op", "?") for o in st.get("do", []))}
               for n, st in enumerate(walk.get("steps", []))]
    steps = [{"i": e.get("i"), "t": e.get("t"), "screen": e.get("screen") or "",
              "label": e.get("label") or "", "mismatch": bool(e.get("mismatch")),
              "stuck": e.get("stuck") or ""} for e in tl.get("events", [])]
    run = read_json(s.root / "inputs" / "run.json", {})
    if run.get("stages") and not RUNS.get(s.root.name, {}).get("alive"):
        for h in run["stages"]:  # a stage left "running" means the runner died mid-stage
            if h["state"] == "running":
                h["state"] = "interrupted"
    for key, what in ARTIFACTS:
        p = s.path(key)
        if p.exists():
            arts.append({"name": p.name, "what": what,
                         "rel": p.relative_to(s.root).as_posix(),
                         "bytes": p.stat().st_size, "masked": "unmasked" not in what})
    prog = parse_progress(s.render / "progress.txt") if (s.render / "progress.txt").exists() else {}
    stale = False
    if rend.get("video") and Path(rend["video"]).exists():
        out_t = Path(rend["video"]).stat().st_mtime
        stale = any(p.exists() and p.stat().st_mtime > out_t
                    for p in (s.path("branding"), s.path("timeline"), s.path("walk")))
    dur = tl.get("duration") or st.get("duration")
    pct = None
    if prog.get("seconds") is not None and dur:
        pct = round(min(100.0, 100.0 * prog["seconds"] / float(dur)), 1)
    return {
        "session": s.root.name, "app": st.get("app"), "package": st.get("package"),
        "device": st.get("device_model"), "serial": st.get("serial"),
        "size": st.get("screen_size"), "stage": status.get("stage")
        or ("ready to share" if rend.get("video") else
            "recorded" if tl else "calibrating"),
        "step": status, "duration": dur, "offset": tl.get("offset"),
        "align": tl.get("align", {}), "chapters": tl.get("chapters", []),
        "boxes": boxes, "screens": tl.get("screens", {}), "run": run,
        "steps": steps, "stale": stale, "walk": steps_w,
        "sections": sec.get("sections", []),
        "sections_total": sec.get("total_duration"),
        "sections_source": sec.get("source"),
        "artifacts": arts, "progress": prog, "percent": pct,
        "proof": (rend.get("proof") or {}).get("tally", {}),
        "video_ready": bool((rend.get("video") or "")) and Path(rend.get("video", "")).exists(),
        "updated": time.strftime("%H:%M:%S"),
    }


def live_view(root: Path) -> dict:
    """What a recipient of a share link may see. Deliberately not session_view: no
    boxes, no walk, no device internals, and no path to the unmasked capture."""
    v = session_view(root)
    tl = read_json(root / "timeline.json", {}) or {}
    return {
        "session": v["session"], "app": v.get("app"), "package": v.get("package"),
        "stage": v.get("stage"), "stale": v.get("stale"),
        "running": bool(RUNS.get(root.name, {}).get("alive")),
        "video_ready": v.get("video_ready"), "duration": v.get("duration"),
        "percent": v.get("percent"), "updated": v.get("updated"),
        "sections": v.get("sections") or [],
        "sections_total": v.get("sections_total"),
        "chapters": [{"title": c.get("title"), "t0": c.get("t0"), "t1": c.get("t1")}
                     for c in tl.get("chapters", [])],
        "report_ready": (root / "render" / "SESSION_DOC.pdf").is_file(),
        "doc_ready": (root / "render" / "SESSION_DOC.md").is_file(),
        "events": SH.recent_events(root, 8),
    }


_UI = Path(__file__).with_name("ui")


def _asset(name: str) -> str:
    return (_UI / name).read_text(encoding="utf-8")


def _compose(page: str, css: str, js: str = "") -> str:
    """Templates live in ui/ so the Python stays readable and the inline script can be
    checked on its own. CSS and JS are inlined because the dashboard serves one file."""
    return (_asset(page)
            .replace("{{CSS}}", _asset("base.css") + "\n" + _asset(css))
            .replace("{{JS}}", _asset(js) if js else ""))


PAGE = _compose("page.html", "app.css", "app.js")
LIVE = _compose("live.html", "live.css", "live.js")
INDEX = _asset("index.html").replace(
    "{{CSS}}", _asset("base.css") + "\n" + _asset("index.css"))


def _frame_size(root: Path) -> list[int]:
    """The frame every mask box is measured against. timeline.json is the authority once
    a take has written it; before that the device size or a still will do. Without any
    size the picker cannot tell a box from a dot and cannot say what is out of frame."""
    tl = read_json(root / "timeline.json", {}) or {}
    if tl.get("size"):
        return list(tl["size"])
    st = read_json(root / "state.json", {}) or {}
    if st.get("screen_size"):
        return list(st["screen_size"])
    for p in sorted((root / "stills").glob("*.png")):
        try:
            return list(ff.dims(p))
        except Exception:
            continue
    return [0, 0]


def _fits(size: list, x: int, y: int, w: int, h: int) -> str | None:
    """A box has to be big enough to mask anything and inside the recorded frame; a drag
    that leaves the frame would silently mask nothing and look like it worked."""
    if w < 8 or h < 8 or x < 0 or y < 0 or (size[0] and x + w > size[0]) or \
       (size[1] and y + h > size[1]):
        return f"box {x},{y} {w}x{h} is not inside the {size[0]}x{size[1]} frame"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "adrd/1"
    timeout = 30  # a stalled chunk PUT must never wedge the single-threaded serve loop

    def log_message(self, *a):  # keep the console readable during a take
        pass

    @property
    def cfg(self):
        return self.server.cfg

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        try:
            self._write(code, body, ctype, extra)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tunnel or a browser tab went away; nothing to report

    def _write(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authed(self, q: dict) -> bool:
        if self.cfg["loopback"] and not self.cfg["token"]:
            return True
        return q.get("t", [""])[0] == self.cfg["token"]

    def _session(self, name: str) -> Path | None:
        root = (self.cfg["dir"] / name).resolve()
        try:
            root.relative_to(self.cfg["dir"].resolve())
        except ValueError:
            return None
        return root if (root / "state.json").exists() else None

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [unquote(p) for p in u.path.split("/") if p]
        if not parts:
            if not self._authed(q):
                return self._send(401, b"token required (see the URL adrd printed)",
                                  "text/plain")
            rows = []
            for d in sorted(self.cfg["dir"].iterdir()):
                if not (d / "state.json").exists():
                    continue
                v = session_view(d)
                apk = (d / "inputs" / "app.apk").is_file()
                scope = UP.read_scope(d)
                sec = read_json(d / "render" / "sections.json", {}) or {}
                href = f"/s/{d.name}/?t={self.cfg['token']}"
                rows.append(
                    f"<tr><td><a href='{href}'>{v['session']}</a></td>"
                    f"<td class='app'>{v['app'] or ''}</td>"
                    f"<td class='n'>{UP.SCOPES[scope]['label']}</td>"
                    f"<td class='{'ok' if apk else 'none'}'>{'apk' if apk else 'none'}</td>"
                    f"<td class='n'>{v['stage']}</td>"
                    f"<td class='n'>{int(v['duration'] or 0)}s</td>"
                    f"<td class='n'>{sec.get('count') or '-'}</td>"
                    f"<td class='{'ok' if v['video_ready'] else 'none'}'>"
                    f"{'yes' if v['video_ready'] else 'not yet'}</td></tr>")
            html = (INDEX.replace("__ROWS__",
                                  "\n".join(rows) or
                                  "<tr><td colspan=8 class=n>No rounds yet.</td></tr>")
                    .replace("__HOST__", "127.0.0.1" if self.cfg["loopback"] else self.cfg["host"]))
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if parts and parts[0] == "live" and len(parts) >= 2:
            return self._live_get(parts[1], parts[2:], q)
        if parts and parts[0] == "s" and len(parts) >= 2:
            return self._session_route(parts[1], parts[2] if len(parts) > 2 else "",
                                       parts[3:], q)
        if parts and parts[0] == "ingest":
            return self._ingest_get(parts, q)
        return self._send(404, b"not found", "text/plain")

    # ---- chunked ingest routes (US-002): /ingest/<session>/... ------------------

    def _ingest_session(self, name: str, q: dict) -> Path | None:
        """Session lookup plus the same token gate every other route uses; None = answered."""
        root = self._session(name)
        if root is None:
            self._send(404, b"no such session", "text/plain")
            return None
        if not self._authed(q):
            self._send(401, b"token required (see the URL adrd printed)", "text/plain")
            return None
        return root

    def _ingest_get(self, parts: list[str], q: dict) -> None:
        if len(parts) == 4 and parts[3] == "status":
            root = self._ingest_session(parts[1], q)
            if root is None:
                return
            code, out = IN.status(root, parts[2])
            return self._send(code, json.dumps(out).encode(), "application/json")
        return self._send(404, b"not found", "text/plain")

    def _json_body(self) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n < 0 or n > IN.MAX_JSON:
            return None
        return self.rfile.read(n)

    def _drain(self, limit: int) -> None:
        got = 0
        while got < limit:
            block = self.rfile.read(min(65536, limit - got))
            if not block:
                break
            got += len(block)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [unquote(p) for p in u.path.split("/") if p]
        if parts and parts[0] == "live" and len(parts) >= 2:
            return self._live_post(parts[1], parts[2:], q)
        if parts and parts[0] == "s" and len(parts) >= 4 and parts[2] == "api":
            return self._session_post(parts[1], parts[3] if len(parts) > 3 else "", q)
        if not parts or parts[0] != "ingest" or len(parts) < 3:
            return self._send(404, b"not found", "text/plain")
        root = self._ingest_session(parts[1], q)
        if root is None:
            return
        body = self._json_body()
        if body is None:
            return self._send(413, b"json body too large", "text/plain")
        if parts[2] == "init" and len(parts) == 3:
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"body must be json"}', "application/json")
            if not isinstance(payload, dict):
                return self._send(400, b'{"error":"body must be a json object"}',
                                  "application/json")
            code, out = IN.init(root, payload)
            return self._send(code, json.dumps(out).encode(), "application/json")
        if len(parts) == 4 and parts[3] == "complete":
            code, out = IN.complete(root, parts[2])
            return self._send(code, json.dumps(out).encode(), "application/json")
        return self._send(404, b"not found", "text/plain")

    def do_PUT(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [unquote(p) for p in u.path.split("/") if p]
        if not parts or parts[0] != "ingest" or len(parts) != 5 or parts[3] != "chunk":
            return self._send(404, b"not found", "text/plain")
        root = self._ingest_session(parts[1], q)
        if root is None:
            return
        try:
            n = int(parts[4])
        except ValueError:
            return self._send(400, b"chunk index must be an integer", "text/plain")
        meta = IN.load_meta(root, parts[2])
        if meta is None:
            return self._send(404, json.dumps({"error": "unknown upload"}).encode(),
                              "application/json")
        try:
            cl = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            cl = -1
        if cl < 0:
            return self._send(411, b"content-length required", "text/plain")
        # reject an oversize body before reading it, so a bad client cannot balloon the heap
        if cl > meta["chunk_size"]:
            self._drain(min(cl, 4 * 1024 * 1024))  # consume a little so the 413 lands
            return self._send(413, json.dumps(
                {"error": f"body {cl} exceeds chunk_size {meta['chunk_size']}"}).encode(),
                "application/json")
        data = self.rfile.read(cl)
        if len(data) != cl:
            return self._send(400, b"truncated body", "text/plain")
        code, out = IN.put_chunk(root, parts[2], n, data)
        self._send(code, json.dumps(out).encode(), "application/json")

    # ---- live link: read-only, share-token scoped, masked artifacts only ---------

    def _live_session(self, name: str, q: dict) -> Path | None:
        root = self._session(name)
        if root is None:
            self._send(404, b"no such session", "text/plain")
            return None
        if not SH.valid(root, q.get("t", [""])[0]):
            self._send(401, b"this live link is not valid - ask for a fresh one",
                       "text/plain")
            return None
        return root

    def _live_get(self, name: str, rest: list[str], q: dict) -> None:
        root = self._live_session(name, q)
        if root is None:
            return
        what = rest[0] if rest else ""
        if what == "":
            return self._send(200, LIVE.encode(), "text/html; charset=utf-8",
                              {"Cache-Control": "no-store"})
        if what == "state":
            view = live_view(root)
            view["share_url"] = SH.link(root, self._base())
            return self._send(200, json.dumps(view).encode(), "application/json",
                              {"Cache-Control": "no-store"})
        if what == "events":
            return self._send(200, json.dumps(SH.recent_events(root)).encode(),
                              "application/json")
        if what == "stream":
            return self._stream(root)
        if what == "f" and len(rest) >= 2:
            rel = "/".join(rest[1:])
            if not SH.shareable(rel):
                return self._send(403, b"not part of the shared results", "text/plain")
            return self._file(root, rel, download=bool(q.get("dl")))
        return self._send(404, b"not found", "text/plain")

    def _live_post(self, name: str, rest: list[str], q: dict) -> None:
        root = self._live_session(name, q)
        if root is None:
            return
        if not rest or rest[0] != "subscribe":
            return self._send(404, b"not found", "text/plain")
        body = self._json_body()
        if body is None:
            return self._send(413, b"json body too large", "text/plain")
        try:
            payload = json.loads(body or b"{}")
            email = (payload.get("email") if isinstance(payload, dict) else "") \
                or q.get("email", [""])[0]
            out = SH.subscribe(root, str(email))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True, **out})

    def _share(self, root: Path, q: dict) -> None:
        trigger = None
        try:
            email = q.get("email", [""])[0]
            if email:
                SH.subscribe(root, email)
                # the recipient asked for the link, so the one email that carries it
                # should actually go out now (or land in the outbox) rather than wait
                trigger = SH.notify(root, "link", base=self._base(), blocking=True,
                                    only=email)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        url = SH.link(root, self._base())
        self._json(200, {"ok": True, "link": url, "subscribers":
                         len(SH.subscribers(root)), "smtp": SH.smtp_configured(),
                         "trigger": trigger})

    # ---- run inputs: the APK under test and the brief it is tested from ----------

    def _session_post(self, name: str, what: str, q: dict) -> None:
        root = self._session(name)
        if root is None:
            return self._send(404, b"no such session", "text/plain")
        if not self._authed(q):
            return self._send(401, b"token required (see the URL adrd printed)", "text/plain")
        if what == "upload":
            return self._upload(root, q)
        if what == "scope":
            return self._scope(root, q)
        if what == "share":
            return self._share(root, q)
        if what == "cases":
            try:
                out = UP.build_cases(root, q.get("scope", [""])[0] or None)
            except UP.UploadError as e:
                return self._send(e.status, json.dumps({"error": str(e)}).encode(),
                                  "application/json")
            return self._send(200, json.dumps({"ok": True, **out}).encode(), "application/json")
        if what == "run":
            return self._run(root, name, q)
        if what == "render":
            return self._render(root, name)
        if what == "export":
            return self._export(root, q)
        if what == "box":
            return self._box(root, q)
        if what == "walk":
            return self._walk(root, q)
        return self._send(404, b"not found", "text/plain")

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _box(self, root: Path, q: dict) -> None:
        """Draw-to-place masking box. Typed pixel coordinates were the failure here: three
        guesses at the wordmark all landed on the wrong band, so the box has to come from
        the still the reviewer is actually looking at."""
        if q.get("op", [""])[0] == "rm":
            return self._box_rm(root, q)
        if q.get("op", [""])[0] == "set":
            return self._box_set(root, q)
        screen = q.get("screen", [""])[0]
        label = (q.get("label", ["brand mark"])[0] or "brand mark")[:60].replace("\n", " ")
        mode = q.get("mode", ["solid"])[0]
        if mode not in ("blur", "pixelate", "solid"):
            return self._json(400, {"error": "mode must be blur, pixelate or solid"})
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", screen):
            return self._json(400, {"error": "unknown screen name"})
        try:
            x, y, w, h = (int(q[k][0]) for k in ("x", "y", "w", "h"))
        except (KeyError, ValueError):
            return self._json(400, {"error": "x, y, w and h are required integers"})
        size = _frame_size(root)
        bad = _fits(size, x, y, w, h)
        if bad:
            return self._json(400, {"error": bad})
        cmd = [sys.executable, _adrd_path(), "brand", "add", "--session", str(root),
               "--screen", screen, "--rect", f"{x},{y},{w},{h}", "--label", label,
               "--mode", mode]
        if size[0]:
            cmd += ["--size", f"{size[0]}x{size[1]}"]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0:
            return self._json(500, {"error": (cp.stdout + cp.stderr).strip()[-300:]})
        self._json(200, {"ok": True, "screen": screen, "rect": [x, y, w, h],
                         "label": label, "mode": mode})

    def _cli(self, root: Path, group: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, _adrd_path(), group, *args, "--session",
                               str(root)], capture_output=True, text=True)

    def _brand(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return self._cli(root, "brand", *args)

    def _box_index(self, root: Path, q: dict) -> int | None:
        """Validate ?i= against the boxes on disk, answering the request itself if it is
        not a number or not one of them."""
        n = len((read_json(root / "branding.json", {}) or {}).get("entries", []))
        try:
            i = int(q.get("i", [""])[0])
        except ValueError:
            self._json(400, {"error": "i must be a box index"})
            return None
        if not 0 <= i < n:
            self._json(400, {"error": f"no box {i} - {n} in the session"})
            return None
        return i

    def _box_rm(self, root: Path, q: dict) -> None:
        """A box that landed on the wrong band has to be removable from the page. Editing
        branding.json by hand while a run is watching it was not workable."""
        i = self._box_index(root, q)
        if i is None:
            return
        entries = (read_json(root / "branding.json", {}) or {}).get("entries", [])
        cp = self._brand(root, "rm", "--index", str(i))
        if cp.returncode != 0:
            return self._json(500, {"error": (cp.stdout + cp.stderr).strip()[-300:]})
        self._json(200, {"ok": True, "removed": i, "label": entries[i].get("label", ""),
                         "left": len(entries) - 1})

    def _box_set(self, root: Path, q: dict) -> None:
        """Move, resize or re-scope an existing box. Every leak that survived a sweep was
        a correct box filed under a screen whose window did not cover the seconds it
        needed, and the fix is a near-miss box - not a redraw of one."""
        screen = q.get("screen", [""])[0]
        if screen and not re.fullmatch(r"global|[A-Za-z0-9_:-]{1,64}", screen):
            return self._json(400, {"error": "unknown screen name"})
        args = ["--screen", screen] if screen else []
        rect = None
        if all(k in q for k in ("x", "y", "w", "h")):
            try:
                x, y, w, h = (int(q[k][0]) for k in ("x", "y", "w", "h"))
            except ValueError:
                return self._json(400, {"error": "x, y, w and h are required integers"})
            size = _frame_size(root)
            bad = _fits(size, x, y, w, h)
            if bad:
                return self._json(400, {"error": bad})
            rect = [x, y, w, h]
            args += ["--rect", ",".join(str(v) for v in rect)]
        if not args:
            return self._json(400, {"error": "nothing to change: pass screen or x,y,w,h"})
        i = self._box_index(root, q)
        if i is None:
            return
        cp = self._brand(root, "set", "--index", str(i), *args)
        if cp.returncode != 0:
            return self._json(500, {"error": (cp.stdout + cp.stderr).strip()[-300:]})
        self._json(200, {"ok": True, "i": i, "scope": screen, "rect": rect})

    def _walk(self, root: Path, q: dict) -> None:
        """Rename a step, re-tag which screen it lands on, or drop it. The screen name is
        what ties a mask box to a stretch of the recording, so a leak fixed on the page
        without this meant editing walk.json in an editor."""
        steps = (read_json(root / "walk.json", {}) or {}).get("steps", [])
        try:
            i = int(q.get("i", [""])[0])
        except ValueError:
            return self._json(400, {"error": "i must be a step index"})
        if not 0 <= i < len(steps):
            return self._json(400, {"error": f"no step {i} - {len(steps)} in this walk"})
        op = q.get("op", ["set"])[0]
        args = ["--index", str(i)]
        if op == "rm":
            pass
        elif op == "set":
            screen = q.get("screen", [""])[0]
            label = q.get("label", [""])[0][:80].replace("\n", " ")
            if screen and not re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", screen):
                return self._json(400, {"error": "unknown screen name"})
            if screen:
                args += ["--screen", screen]
            if label:
                args += ["--label", label]
            if len(args) == 2:
                return self._json(400, {"error": "nothing to change"})
        else:
            return self._json(400, {"error": "op must be set or rm"})
        cp = self._cli(root, "walk", op, *args)
        if cp.returncode != 0:
            return self._json(500, {"error": (cp.stdout + cp.stderr).strip()[-300:]})
        self._json(200, {"ok": True, "i": i, "op": op,
                         "out": (cp.stdout or "").strip().splitlines()[-1:]})

    def _picklist(self, root: Path) -> None:
        tl = read_json(root / "timeline.json", {}) or {}
        seen, out = set(), []
        for e in tl.get("events", []):
            still = (e.get("still") or "").split("/")[-1]
            if still and still not in seen:
                seen.add(still)
                out.append({"still": still, "screen": e.get("screen") or "",
                            "label": e.get("label") or ""})
        # A box is scoped to a screen name, and render masks nothing at all when that name
        # is not in the timeline. So the frames offered here are only the ones whose file
        # is named after a screen the take actually saw.
        known = set(tl.get("screens") or {}) | {e.get("screen") for e in tl.get("events", [])}
        for p in sorted((root / "stills").glob("*.png")):
            if p.name not in seen and p.stem in known:
                seen.add(p.name)
                out.append({"still": p.name, "screen": p.stem, "label": ""})
        self._send(200, json.dumps({"size": _frame_size(root), "stills": out}).encode(),
                   "application/json")

    def _upload(self, root: Path, q: dict) -> None:
        try:
            cl = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            cl = -1
        if cl < 0:
            return self._send(411, b"content-length required", "text/plain")
        try:
            out = UP.stream_to(root, q.get("kind", [""])[0], q.get("name", [""])[0],
                               self.rfile, cl)
        except UP.UploadError as e:
            self._drain(min(cl, 4 * 1024 * 1024))  # consume a little so the error lands
            return self._send(e.status, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        self._send(200, json.dumps({"ok": True, **out}).encode(), "application/json")

    def _scope(self, root: Path, q: dict) -> None:
        try:
            name = UP.write_scope(root, q.get("scope", [""])[0])
        except UP.UploadError as e:
            return self._json(e.status, {"error": str(e)})
        self._json(200, {"ok": True, "scope": name, **UP.SCOPES[name]})

    def _run(self, root: Path, name: str, q: dict) -> None:
        if not (root / "inputs" / "app.apk").is_file():
            return self._json(400, {"error": "upload an .apk first"})
        scope = UP.read_scope(root)
        cases = read_json(root / "cases.json", {}) or {}
        if cases.get("scope") != scope or not cases.get("cases"):
            # the scope on disk and the cases on disk must describe the same run
            try:
                UP.build_cases(root, scope)
            except UP.UploadError as e:
                return self._json(e.status, {"error": str(e)})
        raw = q.get("raw", [""])[0] in ("1", "yes", "true")
        self._start(root, name, raw, base=self._base())

    def _render(self, root: Path, name: str) -> None:
        """Re-run the output stages after a box or caption edit. Without this the page
        kept serving a video that no longer matched the boxes listed beside it."""
        if not (root / "timeline.json").is_file():
            return self._json(400, {"error": "nothing recorded yet - run the round first"})
        self._start(root, name, False,
                    ("retimeline", "render", "sections", "sweep", "doc", "export"),
                    base=self._base())

    def _base(self) -> str:
        """The origin the viewer actually reached, so an emailed link opens for them."""
        env = os.environ.get("ADRD_PUBLIC_URL", "").strip()
        if env:
            return env.rstrip("/")
        origin = getattr(self.server, "public_origin", None)
        if origin:
            return origin.rstrip("/")
        host = self.headers.get("Host") or f"127.0.0.1:{self.cfg['port']}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}"

    def _start(self, root: Path, name: str, raw: bool = False,
               only: tuple[str, ...] | None = None, base: str = "") -> None:
        with RUN_LOCK:
            if RUNS.get(name, {}).get("alive"):
                return self._json(409, {"error": "a run is already going"})
            RUNS[name] = {"alive": True, "base": base}
        threading.Thread(target=_run_guarded,
                         args=(root, name, root / "inputs" / "app.apk", raw, only, base),
                         daemon=True).start()
        if only is None:
            try:
                SH.notify(root, "started", base=base)
            except Exception:
                pass  # a trigger must never block the run it announces
        self._json(202, {"ok": True, "log": "inputs/run.log",
                         "stages": list(only or RUN_STAGES),
                         "export_includes_raw": raw})

    def _inputs(self, root: Path) -> None:
        cases = read_json(root / "cases.json", None)
        scope = UP.read_scope(root)
        out = UP.listed(root)
        out["scope"] = {"name": scope, **UP.SCOPES[scope]}
        out["scopes"] = UP.SCOPES
        out["cases"] = None if cases is None else {
            "origin": cases.get("origin"), "count": len(cases.get("cases", [])),
            "scope": cases.get("scope"), "counts": cases.get("counts") or {},
            "sections": cases.get("sections") or [],
            "stale": cases.get("scope") != scope}
        out["running"] = bool(RUNS.get(root.name, {}).get("alive"))
        log = root / "inputs" / "run.log"
        out["log"] = log.read_text(encoding="utf-8", errors="replace")[-4000:] \
            if log.is_file() else ""
        self._send(200, json.dumps(out).encode(), "application/json")

    def _session_route(self, name: str, what: str, rest: list[str], q: dict) -> None:
        root = self._session(name)
        if root is None:
            return self._send(404, b"no such session", "text/plain")
        if not self._authed(q):
            return self._send(401, b"token required (see the URL adrd printed)", "text/plain")
        if what == "":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if what == "state":
            return self._send(200, json.dumps(session_view(root)).encode(), "application/json")
        if what == "stream":
            return self._stream(root)
        if what == "f":
            return self._file(root, "/".join(rest) or q.get("p", [""])[0],
                              download=bool(q.get("dl")))
        if what == "evidences":
            return self._send(200, json.dumps(evidence_list(root)).encode(),
                              "application/json")
        if what == "images.zip":
            return self._zip(root)
        if what == "api" and rest and rest[0] == "export":
            return self._export(root, q)
        if what == "api" and rest and rest[0] == "inputs":
            return self._inputs(root)
        if what == "api" and rest and rest[0] == "picklist":
            return self._picklist(root)
        if what == "api" and rest and rest[0] == "share":
            tok = SH.token(root)
            return self._send(200, json.dumps({
                "ok": True, "configured": bool(tok),
                "link": SH.link(root, self._base()) if tok else None,
                "subscribers": len(SH.subscribers(root)) if tok else 0,
                "smtp": SH.smtp_configured(),
                "events": SH.recent_events(root, 8)}).encode(), "application/json")
        return self._send(404, b"not found", "text/plain")

    def _file(self, root: Path, rel: str, download: bool = False) -> None:
        p = (root / unquote(rel)).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            return self._send(403, b"outside the session", "text/plain")
        if not p.is_file():
            return self._send(404, b"missing", "text/plain")
        if RAW_MARK in rel and not self.cfg["allow_unmasked"]:
            return self._send(403, b"unmasked capture is not served while this "
                                 b"dashboard is reachable off this machine; run "
                                 b"'adrd watch --allow-unmasked' if you own it",
                             "text/plain")
        ctype = {"mp4": "video/mp4", "vtt": "text/vtt", "png": "image/png",
                 "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "pdf": "application/pdf",
                 "md": "text/markdown; charset=utf-8", "json": "application/json",
                 "txt": "text/plain; charset=utf-8"}.get(p.suffix.lstrip("."), "application/octet-stream")
        data = p.read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and ctype == "video/mp4":
            try:
                start_s, _, end_s = rng[6:].partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s else len(data) - 1
            except ValueError:
                return self._send(416, b"", "text/plain")
            end = min(end, len(data) - 1)
            if start > end:
                return self._send(416, b"", "text/plain")
            return self._send(206, data[start:end + 1], ctype,
                              {"Content-Range": f"bytes {start}-{end}/{len(data)}",
                               "Accept-Ranges": "bytes"})
        headers = {"Accept-Ranges": "bytes"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
        self._send(200, data, ctype, headers)

    def _stream(self, root: Path) -> None:
        frames: Frames = self.server.frames
        if frames.jpeg is None:
            still = sorted((root / "stills").glob("*.png")) if (root / "stills").exists() else []
            if still:
                return self._send(200, still[-1].read_bytes(), "image/png")
            return self._send(503, b"no device and no stills yet", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = -1
        deadline = time.time() + self.cfg["stream_ttl"]
        try:
            while time.time() < deadline and not self.server.closing:
                if frames.seq == last or frames.jpeg is None:
                    time.sleep(0.15)
                    continue
                last = frames.seq
                self.wfile.write(f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                 f"Content-Length: {len(frames.jpeg)}\r\n\r\n".encode())
                self.wfile.write(frames.jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _zip(self, root: Path) -> None:
        import io
        import zipfile as zf
        buf = io.BytesIO()
        with zf.ZipFile(buf, "w", zf.ZIP_DEFLATED) as z:
            for f in evidence_files(root):
                z.write(f, f.relative_to(root))
        data = buf.getvalue()
        name = f"{root.name}-evidences.zip"
        self._send(200, data, "application/zip", {"Content-Disposition":
                                                   f'attachment; filename="{name}"'})

    def _export(self, root: Path, q: dict) -> None:
        from . import export as X
        s = Session(root)
        # off unless asked: a bundle that quietly carries the unmasked capture is the
        # one leak this tool cannot undo once it leaves the machine.
        raw = q.get("raw", [""])[0] in ("1", "yes", "true")
        try:
            X.build(s, include_raw=raw, dest=str(root / "export"))
        except Exception as exc:
            return self._send(500, str(exc).encode(), "text/plain")
        self._json(200, {"ok": True, "raw": raw,
                         "zip": (root / "export.zip").stat().st_size})

    do_HEAD = do_GET


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threading matters: the live view holds an open /stream for as long as a tab is
    looking at it. On a plain TCPServer that one connection blocks every other request,
    so opening the dashboard appeared to take the whole tool down."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, cfg: dict):
        self.cfg = cfg
        super().__init__((cfg["host"], cfg["port"]), Handler)
        self.frames = Frames(cfg.get("adb"))
        self.closing = False
        self.public_url = None
        self.public_origin = None

    def serve_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown_all(self) -> None:
        self.closing = True
        self.frames.stop()
        self.shutdown()
        self.server_close()


def daemonize(argv: list[str], log: Path) -> int:
    """Relaunch in a new session. nohup+disown still dies with the shell's process group."""
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, *argv],
                            stdout=open(log, "a"), stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def pick_port(host: str, want: int, tries: int = 10) -> int:
    """The URL is only 'always the same' if we can land on the same port."""
    for port in range(want, want + tries):
        try:
            probe = socketserver.TCPServer((host, port), Handler)
        except OSError:
            continue
        probe.server_close()
        return port
    raise OSError(f"ports {want}-{want + tries - 1} are all busy")


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def tunnel_record(sessions_dir: Path) -> Path:
    return Path(sessions_dir) / "tunnel.json"


def tunnel_status(sessions_dir: Path, port: int | None = None) -> str | None:
    """The hostname only stays fixed if the same cloudflared process keeps running.

    When a port is given, a recorded tunnel pointing at a *different* local port is not
    reusable - handing it out would print a URL that 502s, because cloudflared forwards to
    the origin it was started with, not to whatever port this call ended up on.
    """
    d = read_json(tunnel_record(sessions_dir), None)
    if not (d and d.get("url") and _alive(d.get("pid"))):
        return None
    if port is not None:
        local = str(d.get("local") or "")
        if local and not local.endswith(f":{port}"):
            try:
                os.kill(int(d["pid"]), 15)
            except (OSError, ValueError):
                pass
            return None
    return d["url"]


def tunnel_config(sessions_dir: Path, name: str, uuid: str, hostname: str,
                  local_url: str) -> Path:
    """A named tunnel needs an ingress file; --url only exists for quick tunnels."""
    p = sessions_dir / f"{name}.yml"
    p.write_text(
        f"tunnel: {uuid}\n"
        f"credentials-file: {cf_dir() / (name + '.json')}\n"
        f"ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: {local_url}\n"
        f"  - service: http_status:404\n", encoding="utf-8")
    return p


def named_tunnel(local_url: str, log: Path, sessions_dir: Path, hostname: str) -> str | None:
    """A fixed hostname that survives restarts, so the URL you bookmark is the URL you
    open tomorrow. Needs one `cloudflared tunnel login` and a zone in that account."""
    name = re.sub(r"[^a-z0-9-]", "-", hostname.split(".")[0])[:40] or "adrd"
    cred = cf_dir() / f"{name}.json"
    with log.open("a") as fh:
        if not cred.exists():
            # cloudflared's default filename is not the tunnel name, so say where to put
            # the credential instead of guessing it and re-creating the tunnel each start
            cp = subprocess.run(["cloudflared", "tunnel", "create", name,
                                 "--credentials-file", str(cred)],
                                stdout=fh, stderr=subprocess.STDOUT, text=True)
            if cp.returncode != 0 or not cred.exists():
                return None
        uuid = json.loads(cred.read_text()).get("tunnel_id") or name
        cfg = tunnel_config(sessions_dir, name, uuid, hostname, local_url)
        if not (sessions_dir / f"{name}.routed").exists():
            cp = subprocess.run(["cloudflared", "tunnel", "route", "dns", "--overwrite-dns",
                                 name, hostname],
                                stdout=fh, stderr=subprocess.STDOUT, text=True)
            if cp.returncode != 0:
                return None
            (sessions_dir / f"{name}.routed").write_text(hostname)
        proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate",
                                 "--config", str(cfg), "run"],
                                stdout=fh, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        if _alive(proc.pid):
            tunnel_record(sessions_dir).write_text(json.dumps(
                {"url": f"https://{hostname}", "pid": proc.pid, "local": local_url,
                 "tunnel_name": name, "named": True,
                 "since": time.strftime("%Y-%m-%d %H:%M:%S")}))
            return f"https://{hostname}"
        time.sleep(0.5)
    return None


def tunnel(local_url: str, log: Path, sessions_dir: Path,
           hostname: str | None = None) -> str | None:
    """Expose the dashboard through Cloudflare.

    With a hostname and a Cloudflare login this is a named tunnel: the URL is yours and
    does not change when the process restarts. Without them it is a quick tunnel - no
    account and no install, but the hostname is random and dies with the process, so
    nothing about it can be bookmarked.
    """
    if not shutil.which("cloudflared"):
        print("cloudflared not found; install it or use --lan", file=sys.stderr)
        return None
    if hostname:
        if not cf_cert().exists():
            print(f"  no Cloudflare login at {cf_cert()} - cannot bind {hostname}.\n"
                  f"  one-time: cloudflared tunnel login   (opens a browser)\n"
                  f"  falling back to a quick tunnel with a throwaway hostname",
                  file=sys.stderr)
        else:
            got = named_tunnel(local_url, log, sessions_dir, hostname)
            if got:
                return got
            print("  named tunnel did not come up; see the dashboard log", file=sys.stderr)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "w")
    proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", local_url],
                            stdout=fh, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text())
        except OSError:
            m = None
        if m:
            tunnel_record(sessions_dir).write_text(json.dumps(
                {"url": m.group(), "pid": proc.pid, "local": local_url,
                 "since": time.strftime("%Y-%m-%d %H:%M:%S")}))
            return m.group()
        time.sleep(0.5)
    return None


def _bindable(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _tunnel_port(sessions_dir: Path, host: str, port: int) -> int:
    """A quick tunnel's hostname belongs to the cloudflared process, not to this one, so
    the only way to keep the link you already shared is to keep serving the port it
    forwards to. Bumping to the next free port would mint a brand new hostname."""
    rec = read_json(tunnel_record(sessions_dir), {}) or {}
    if not _alive(rec.get("pid")):
        return port
    try:
        tp = int(str(rec.get("local") or "").rsplit(":", 1)[-1])
    except ValueError:
        return port
    if not tp:
        return port
    deadline = time.time() + 8
    while not _bindable(host, tp) and time.time() < deadline:
        time.sleep(0.4)
    if not _bindable(host, tp):
        # restarting too soon finds the previous watcher's socket still held; jumping to
        # the next port here would kill a tunnel whose link people already have
        print(f"  port {tp} is busy while a live tunnel points at it - a new hostname "
              "is about to be issued", flush=True)
        return port
    if tp != port:
        print(f"  keeping port {tp} for the live tunnel so the URL does not change",
              flush=True)
    return tp


def start(sessions_dir: Path, port: int = 8734, host: str = "127.0.0.1",
          token: str | None = None, adb=None, focus: str | None = None,
          open_browser: bool = True, public: bool = False,
          expires_h: float = 6.0, allow_unmasked: bool = False,
          hostname: str | None = None) -> Server:
    """Start the dashboard. Returns the server; call shutdown_all() when done."""
    sessions_dir = Path(sessions_dir).resolve()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if public:
        port = _tunnel_port(sessions_dir, host, port)
        rec = read_json(tunnel_record(sessions_dir), {}) or {}
        # the link already shared carries its own token, so reusing the hostname without
        # it would hand everyone a URL that 401s
        if not token and _alive(rec.get("pid")) and rec.get("token") and \
           str(rec.get("local", "")).endswith(f":{port}"):
            token = rec["token"]
            print("  reusing the shared link's token as well", flush=True)
    # A tunnel reaches a loopback bind just fine, so `--public` must force the token
    # itself; relying on the bind to decide would leave the whole thing wide open.
    if (not loopback or public) and not token:
        token = secrets.token_urlsafe(8)
    port = pick_port(host, port)
    cfg = {"dir": sessions_dir, "host": host, "port": port, "token": token or "",
           "loopback": loopback and not public, "adb": adb, "stream_ttl": 300,
           # off this machine the raw capture is never served unless the owner asks for it
           "allow_unmasked": allow_unmasked or (loopback and not public)}
    srv = Server(cfg)
    srv.serve_thread()
    base = f"http://{'127.0.0.1' if loopback and not public else _lan_ip()}:{port}"
    url = f"{base}/" + (f"?t={token}" if token else "")
    print(f"dashboard {url}", flush=True)
    if focus:
        print(f"  session page {base}/s/{focus}/" + (f"?t={token}" if token else ""),
              flush=True)
    if public:
        rec = read_json(tunnel_record(sessions_dir), {}) or {}
        pub = tunnel_status(sessions_dir, port)
        if pub and bool(rec.get("named")) != bool(hostname):
            pub = None  # a throwaway quick URL is not what --hostname asked for
        if pub:
            print(f"  reusing the running tunnel {pub}", flush=True)
        else:
            pub = tunnel(f"http://127.0.0.1:{port}", sessions_dir / "tunnel.log",
                         sessions_dir, hostname=hostname)
        if pub:
            page = f"{pub}/s/{focus}/?t={token}" if focus else f"{pub}/?t={token}"
            print(f"PUBLIC {page}", flush=True)
            srv.public_url = page
            srv.public_origin = pub
            d = read_json(tunnel_record(sessions_dir), {}) or {}
            if d.get("token") != token:  # so the next restart can reuse this whole link
                d["token"] = token
                tunnel_record(sessions_dir).write_text(json.dumps(d))
            if expires_h:
                threading.Timer(expires_h * 3600, srv.shutdown_all, ()).start()
                print(f"  auto-closes in {expires_h:g}h", flush=True)
        else:
            print("  tunnel did not come up; see tunnel.log", file=sys.stderr)
    elif not loopback:
        print("  WARNING: bound off-loopback. Anyone reachable on this network with the "
              "URL sees unmasked client app screens while a take is running.", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return srv


def _lan_ip() -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def write_status(session_root: Path, **kw) -> None:
    kw["at"] = round(time.time(), 2)
    kw["ts"] = time.strftime("%H:%M:%S")
    (Path(session_root) / "status.json").write_text(json.dumps(kw))
