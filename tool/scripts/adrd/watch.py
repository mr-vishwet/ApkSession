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
import socketserver
import subprocess
import sys
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import ingest as IN
from .session import Session

BOUNDARY = "adrdframe"
RAW_MARK = "recording"
ARTIFACTS = [("final", "shareable video"), ("vtt", "chapters"), ("doc", "session doc"),
             ("recording", "raw capture (unmasked)"), ("sheet", "screen sheet"),
             ("proof", "region proof"), ("bundle", "final export bundle")]
LIVE_TTL = 45.0  # a status file this old means nothing is running


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
    return [{"rel": str(f.relative_to(root)), "bytes": f.stat().st_size}
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
    branding = read_json(s.path("branding"), {"entries": []})
    boxes = []
    for e in branding.get("entries", []):
        frac = e.get("frac") or {}
        boxes.append({"scope": e.get("scope", ""), "label": e.get("label", ""),
                      "mode": e.get("mode", "blur"),
                      "frac": [frac.get("x", 0), frac.get("y", 0),
                               frac.get("w", 0), frac.get("h", 0)]})
    arts = []
    for key, what in ARTIFACTS:
        p = s.path(key)
        if p.exists():
            arts.append({"name": p.name, "what": what, "rel": str(p.relative_to(s.root)),
                         "bytes": p.stat().st_size, "masked": "unmasked" not in what})
    prog = parse_progress(s.render / "progress.txt") if (s.render / "progress.txt").exists() else {}
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
        "boxes": boxes, "artifacts": arts, "progress": prog, "percent": pct,
        "proof": (rend.get("proof") or {}).get("tally", {}),
        "video_ready": bool((rend.get("video") or "")) and Path(rend.get("video", "")).exists(),
        "updated": time.strftime("%H:%M:%S"),
    }


PAGE = """<!doctype html><meta charset=utf-8><title>adrd</title>
<style>
:root{--bg:#0e1116;--fg:#e6e9ef;--mut:#8b94a3;--ln:#1e242e;--ac:#5ac8fa;--ok:#34c759;--warn:#ff9500;--bad:#ff3b30}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}
header{display:flex;gap:14px;align-items:baseline;padding:14px 20px;border-bottom:1px solid var(--ln);flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}header span{color:var(--mut);font-size:12px}
main{display:grid;grid-template-columns:minmax(280px,340px) 1fr;gap:20px;padding:20px;align-items:start}
.stage{border:1px solid var(--ln);border-radius:10px;padding:14px;margin-bottom:14px}
.stage h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px}
.view{position:relative;background:#000;border-radius:10px;overflow:hidden;border:1px solid var(--ln)}
.view img{display:block;width:100%}
.box{position:absolute;border:2px solid var(--bad);border-radius:3px}
.box i{position:absolute;top:-19px;left:0;background:var(--bad);color:#000;font-size:10px;padding:1px 5px;border-radius:3px;font-style:normal;white-space:nowrap}
.rail{display:flex;flex-direction:column;gap:2px}
.ch{display:flex;gap:10px;padding:6px 8px;border-radius:6px;color:var(--mut)}
.ch b{color:var(--fg);font-weight:500;flex:1}
.ch.on{background:#161d27}.ch.on b{color:var(--ac)}
.bar{height:6px;background:var(--ln);border-radius:3px;overflow:hidden;margin:8px 0}
.bar i{display:block;height:100%;background:var(--ac)}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:5px 8px;border-top:1px solid var(--ln);vertical-align:top}
td:first-child{color:var(--mut);white-space:nowrap}
a{color:var(--ac);text-decoration:none}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;background:#161d27;color:var(--mut)}
.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.bad{color:var(--bad)}
pre{white-space:pre-wrap;color:var(--mut);font-size:12px;margin:0}
.maskbox{position:absolute;overflow:hidden;backdrop-filter:blur(18px);
 -webkit-backdrop-filter:blur(18px);border:1px solid rgba(255,149,0,.7)}
.row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
button{background:#161d27;color:var(--fg);border:1px solid var(--ln);border-radius:6px;
 padding:6px 12px;font:inherit;cursor:pointer}
button:hover{border-color:var(--ac)}
video{width:100%;max-height:52vh;background:#000;border-radius:8px}
.evi{max-height:340px;overflow:auto;display:flex;flex-direction:column;gap:4px}
.evi a{font-size:12px;color:var(--mut);text-decoration:none}
.evi a:hover{color:var(--ac)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{border:1px solid var(--ln);border-radius:10px;padding:12px}
.card b{display:block}.card span{color:var(--mut);font-size:12px}
.warn{background:#2a1e0b;border:1px solid #5c4313;border-radius:8px;padding:10px 12px;color:#ffcc80;font-size:13px}
</style>
<header><h1 id=t>loading</h1><span id=d></span><span id=dev></span>
<span class=pill id=stage></span><span style="flex:1"></span>
<label class=row style=margin:0><input type=checkbox id=mt checked>mask preview</label>
<button id=ex>Export all</button><a href="/">all sessions</a></header>
<main><div>
<div class=stage><h2>Live device</h2><div class=view id=v><img id=live alt=preview></div>
<div id=err></div></div>
<div class=stage><h2>Stage</h2><div id=step></div><div class=bar><i id=pb style=width:0></i></div><div id=pct></div></div>
<div class=stage><h2>Rendered video</h2>
<div class=row><label><input type=radio name=v checked id=vMask> masked</label>
<label><input type=radio name=v id=vRaw> unmasked</label></div>
<video id=vid controls playsinline></video><div id=vinfo></div></div>
</div><div>
<div class=stage><h2>Chapters</h2><div class=rail id=rail></div></div>
<div class=stage><h2>Artifacts</h2><div id=arts></div></div>
<div class=stage><h2>Masking</h2><div id=mask></div></div>
<div class=stage><h2>Evidences <button id=allimg style=padding:2px 8px>download all</button></h2>
<div class=evi id=evi></div></div>
</div></main>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
const S=location.pathname.replace(/\\/$/,'');
const u=(p,q)=>{const o=Object.assign({t:T},q||{});return S+p+'?'+
  Object.keys(o).map(k=>k+'='+encodeURIComponent(o[k])).join('&')};
let boxes=[];
function el(t,a,x){const e=document.createElement(t);Object.assign(e,a||{});if(x)e.innerHTML=x;return e}
function draw(){const v=document.getElementById('v');v.querySelectorAll('.box').forEach(b=>b.remove());
 if(!v.clientWidth)return;const w=v.clientWidth;
 boxes.forEach(b=>{const f=b.frac;if(f[2]<=0)return;const d=el('div',{className:'box'});
  d.className='box'+(masked?' maskbox':'');
  d.style.cssText=`left:${f[0]*100}%;top:${f[1]*100}%;width:${f[2]*100}%;height:${f[3]*100}%`;
  d.appendChild(el('i',{},b.label||''));v.appendChild(d)})}
let masked=true, ART={};
document.getElementById('mt').onchange=e=>{masked=e.target.checked;draw()};
document.getElementById('ex').onclick=async e=>{e.target.disabled=1;e.target.textContent='exporting...';
 const r=await (await fetch(u('/api/export'))).json().catch(()=>({ok:false}));
 e.target.disabled=0;e.target.textContent=r.ok?'exported':'export failed';
 setTimeout(()=>{e.target.textContent='Export all';tick()},1200)};
document.getElementById('allimg').onclick=()=>location=u('/images.zip');
async function tick(){let j;
 try{j=await (await fetch(u('/state'))).json()}
 catch(e){document.getElementById('step').innerHTML='<pre>state fetch failed: '+e+
   '</pre>';return}
 document.getElementById('t').textContent=j.app||j.session;
 document.getElementById('d').textContent=(j.package||'')+(j.duration?' - '+Math.round(j.duration)+'s':'');
 document.getElementById('dev').textContent=j.device?`${j.device} ${j.size?j.size.join('x'):''}`:'no device';
 const st=document.getElementById('stage');st.textContent=j.stage;st.className='pill '+(j.stage.includes('shar')?'ok':'warn');
 const s=j.step||{};
 document.getElementById('step').innerHTML=s.i!==undefined
  ?`<b>${(s.chapter||'-')}</b><br><span style=color:var(--mut)>step ${s.i} · ${s.label||''}</span>`
  :'<pre>idle - nothing running right now</pre>';
 document.getElementById('pb').style.width=(j.percent||0)+'%';
 document.getElementById('pct').textContent=j.percent!=null?`render ${j.percent}%`:'';
 const cur=s.i!==undefined?j.chapters.findIndex(c=>s.chapter&&c.module===s.chapter):-1;
 document.getElementById('rail').innerHTML=j.chapters.map((c,i)=>
  `<div class="ch ${i===cur?'on':''}"><span>${ff(c.t0)}-${ff(c.t1)}</span><b>${c.module}</b><span>${c.opened_with||''}</span></div>`).join('')||'<pre>no timeline yet</pre>';
 document.getElementById('arts').innerHTML='<table>'+j.artifacts.map(a=>
  `<tr><td>${a.masked?`<a href="${u('/f/'+a.rel+'&dl=1')}">${a.name}</a>`:a.name}</td>`
  +`<td>${a.what}</td><td>${kb(a.bytes)}</td>`
  +`<td>${a.masked?'':'<span class="pill bad">unmasked</span>'}</td></tr>`).join('')+'</table>';
 boxes=j.boxes;draw();
 ART={};j.artifacts.forEach(a=>ART[a.name]=a.rel);
 const want=document.getElementById('vRaw').checked?'recording.mp4':'demo_debranded.mp4';
 const vid=document.getElementById('vid');
 if(ART[want]&&vid.dataset.rel!==ART[want]){vid.dataset.rel=ART[want];
   vid.src=u('/f/'+ART[want],{dl:1});vid.load()}
 document.getElementById('vinfo').textContent=ART[want]?`${want} - ${kb((j.artifacts.find(a=>a.name===want)||{}).bytes||0)}`:'not built yet';
 if(!document.getElementById('evi').dataset.n){document.getElementById('evi').dataset.n=1;
  fetch(u('/evidences')).then(r=>r.json()).then(l=>{document.getElementById('evi').innerHTML=
   l.slice(0,400).map(e=>`<a href="${u('/f/'+e.rel,{dl:1})}" target=_blank>${e.rel}</a>`).join('')+
   (l.length>400?`<span class=n>...${l.length-400} more - use download all</span>`:'')})}
 const p=j.proof||{},bad=Object.keys(p).filter(k=>k.startsWith('unchanged'));
 document.getElementById('mask').innerHTML=`<div>${j.boxes.length} box(es)</div>`+
  Object.entries(p).map(([k,v])=>`<span class="pill ${k.startsWith('strongly')?'ok':k.startsWith('unchanged')?'bad':''}">${v} ${k}</span>`).join(' ')+
  (bad.length?`<div class=warn style=margin-top:8px>${bad.length} box covers nothing - fix its screen name or rect</div>`:'')+
  '<div class=warn style=margin-top:10px>Everything on this page is UNMASKED '
  +'client content. The only shareable file is the rendered mp4.</div>';
 const live=document.getElementById('live'),err=document.getElementById('err');
 if(!live.dataset.on){live.dataset.on=1;live.src=u('/stream')}
 err.innerHTML=live.dataset.err?`<pre>${live.dataset.err}</pre>`:''}
document.getElementById('vMask').onchange=tick;
document.getElementById('vRaw').onchange=tick;
function ff(s){s=Math.max(0,s||0);return String(Math.floor(s/60)).padStart(2,'0')+':'+String(Math.floor(s%60)).padStart(2,'0')}
function kb(n){return n>1e6?(n/1e6).toFixed(1)+' MB':Math.round(n/1024)+' KB'}
tick();setInterval(tick,1500);setInterval(draw,900);
</script>"""

INDEX = """<!doctype html><meta charset=utf-8><title>adrd sessions</title>
<style>body{background:#0e1116;color:#e6e9ef;font:14px/1.6 -apple-system,system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:16px}a{color:#5ac8fa;text-decoration:none}
table{border-collapse:collapse;width:100%;max-width:900px}td{padding:8px;border-top:1px solid #1e242e;vertical-align:top}
td:first-child{color:#8b94a3;white-space:nowrap}.n{color:#8b94a3;font-size:12px}</style>
<h1>Recording sessions</h1>
<table><tr><td>session</td><td>app</td><td>stage</td><td>length</td><td>shareable</td></tr>
__ROWS__</table>
<p class=n>Serve is bound to __HOST__. Live frames are unmasked client app content - a non-loopback
bind requires the token in the URL.</p>"""


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
                href = f"/s/{d.name}/?t={self.cfg['token']}"
                rows.append(f"<tr><td><a href='{href}'>{v['session']}</a></td>"
                            f"<td>{v['app'] or ''}</td><td>{v['stage']}</td>"
                            f"<td>{int(v['duration'] or 0)}s</td>"
                            f"<td>{'yes' if v['video_ready'] else 'not yet'}</td></tr>")
            html = (INDEX.replace("__ROWS__",
                                  "\n".join(rows) or "<tr><td colspan=5>none yet</td></tr>")
                    .replace("__HOST__", "127.0.0.1" if self.cfg["loopback"] else self.cfg["host"]))
            return self._send(200, html.encode(), "text/html; charset=utf-8")
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
            return self._export(root)
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

    def _export(self, root: Path) -> None:
        from . import export as X
        s = Session(root)
        try:
            X.build(s, include_raw=True, dest=str(root / "export"))
        except Exception as exc:
            return self._send(500, str(exc).encode(), "text/plain")
        self._send(200, json.dumps({"ok": True,
                                    "zip": str((root / "export.zip").stat().st_size)}).encode(),
                   "application/json")

    do_HEAD = do_GET


class Server(socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        super().__init__((cfg["host"], cfg["port"]), Handler)
        self.frames = Frames(cfg.get("adb"))
        self.closing = False
        self.public_url = None

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


def tunnel_status(sessions_dir: Path) -> str | None:
    """The hostname only stays fixed if the same cloudflared process keeps running."""
    d = read_json(tunnel_record(sessions_dir), None)
    if d and d.get("url") and _alive(d.get("pid")):
        return d["url"]
    return None


def tunnel(local_url: str, log: Path, sessions_dir: Path) -> str | None:
    """Expose the dashboard through a Cloudflare quick tunnel (no account, no install).

    The URL is random and dies with this process. Nothing is registered or persisted.
    """
    if not shutil.which("cloudflared"):
        print("cloudflared not found; install it or use --lan", file=sys.stderr)
        return None
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


def start(sessions_dir: Path, port: int = 8734, host: str = "127.0.0.1",
          token: str | None = None, adb=None, focus: str | None = None,
          open_browser: bool = True, public: bool = False,
          expires_h: float = 6.0, allow_unmasked: bool = True) -> Server:
    """Start the dashboard. Returns the server; call shutdown_all() when done."""
    sessions_dir = Path(sessions_dir).resolve()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    # A tunnel reaches a loopback bind just fine, so `--public` must force the token
    # itself; relying on the bind to decide would leave the whole thing wide open.
    if (not loopback or public) and not token:
        token = secrets.token_urlsafe(8)
    port = pick_port(host, port)
    cfg = {"dir": sessions_dir, "host": host, "port": port, "token": token or "",
           "loopback": loopback and not public, "adb": adb, "stream_ttl": 300,
           "allow_unmasked": allow_unmasked}
    srv = Server(cfg)
    srv.serve_thread()
    base = f"http://{'127.0.0.1' if loopback and not public else _lan_ip()}:{port}"
    url = f"{base}/" + (f"?t={token}" if token else "")
    print(f"dashboard {url}", flush=True)
    if focus:
        print(f"  session page {base}/s/{focus}/" + (f"?t={token}" if token else ""),
              flush=True)
    if public:
        pub = tunnel_status(sessions_dir)
        if pub:
            print(f"  reusing the running tunnel {pub}", flush=True)
        else:
            pub = tunnel(f"http://127.0.0.1:{port}", sessions_dir / "tunnel.log",
                         sessions_dir)
        if pub:
            page = f"{pub}/s/{focus}/?t={token}" if focus else f"{pub}/?t={token}"
            print(f"PUBLIC {page}", flush=True)
            srv.public_url = page
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
