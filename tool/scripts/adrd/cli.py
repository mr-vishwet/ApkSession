"""Stage-by-stage CLI. Each command reads the session dir and writes the next record."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import docgen, export as X, ff, flowmap, render as R, timeline as T
from .adb import Adb, Recorder
from .env import doctor, find_flutter, list_serials
from .img import annotate, contact_sheet
from .session import Session, slugify
from . import watch as W


def _watch(a, s: Session, adb: Adb | None = None):
    """Start the dashboard in-process so it is live for the duration of this command."""
    if not getattr(a, "watch", False):
        return None
    host = "0.0.0.0" if a.lan else a.host
    return W.start(s.root.parent, port=a.port, host=host, token=a.token, adb=adb,
                   focus=s.root.name, open_browser=not a.no_browser,
                   public=getattr(a, "public", False), expires_h=a.expires,
                   allow_unmasked=not getattr(a, "hide_unmasked", False))


def _linger(a, srv) -> None:
    if srv is None:
        return
    secs = getattr(a, "keep_alive", 60)
    print(f"dashboard still up for {secs}s (Ctrl-C to stop now)")
    try:
        time.sleep(secs)
    except KeyboardInterrupt:
        pass
    srv.shutdown_all()


def cmd_watch(a) -> int:
    """Serve the dashboard on its own: browse every session, watch a running task."""
    s = Session(a.session)
    if getattr(a, "daemon", False) and "--daemon" in sys.argv:
        argv = [x for x in sys.argv[1:] if x != "--daemon"]
        pid = W.daemonize([str(Path(__file__).resolve().parent.parent / "adrd.py"), *argv],
                          s.root / "dashboard.log")
        print(f"dashboard detached as pid {pid}; log {s.root / 'dashboard.log'}")
        return 0
    adb = None
    if a.device:
        try:
            adb = _adb(a)
            _stamp(adb, s)
        except Exception as exc:
            print(f"live preview off: {exc}", file=sys.stderr)
    srv = W.start(s.root.parent, port=a.port, host="0.0.0.0" if a.lan else a.host,
                  token=a.token, adb=adb, focus=s.root.name,
                  open_browser=not a.no_browser, public=a.public, expires_h=a.expires,
                  allow_unmasked=not a.hide_unmasked,
                  hostname=getattr(a, "hostname", None))
    if srv.public_url:
        s.update_state(public_url=srv.public_url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown_all()
    return 0


def cmd_doctor(a) -> int:
    ps = doctor()
    for p in ps:
        print(f"{'ok  ' if p.ok else 'MISS'} {p.name:<18} {p.detail}"
              + (f"\n       fix: {p.fix}" if p.fix and not p.ok else ""))
    bad = [p for p in ps if not p.ok and p.name in ("adb", "ffmpeg", "ffmpeg filters", "pillow")]
    return 1 if bad else 0


def _adb(a) -> Adb:
    return Adb(serial=getattr(a, "serial", None))


def _stamp(adb: Adb, s: Session) -> None:
    """Record which device produced this session; the walk and boxes depend on its size."""
    s.update_state(serial=adb.device, device_model=adb.model(),
                   screen_size=list(adb.screen_size()))


def cmd_devices(a) -> int:
    print("\n".join(list_serials()) or "no devices")
    return 0


def cmd_url(a) -> int:
    """Answer "what is the link" without guessing which of the printed lines is current.

    A quick-tunnel hostname belongs to the cloudflared process, so a recorded URL whose
    process is gone is a dead link, not an old one - saying so is the useful part."""
    s = Session(a.session)
    url = s.state().get("public_url") or ""
    live = W.tunnel_status(s.root.parent)
    if not url:
        print("no public URL recorded for this session - start one with "
              "adrd watch --session DIR --public", file=sys.stderr)
        return 1
    if not live:
        print(f"the tunnel behind {url} is not running - restart watch --public and "
              "a new hostname will be issued", file=sys.stderr)
        return 1
    print(url if url.startswith(live) else f"{url}\n  note: this tunnel now answers at {live}")
    return 0


def cmd_shot(a) -> int:
    adb, s = _adb(a), Session(a.session)
    _stamp(adb, s)
    W, H = adb.screen_size()
    name = a.name or f"shot_{int(time.time())}"
    p = s.stills / f"{name}.png"
    adb.screencap(p)
    print(f"{p}  {W}x{H}  focus={adb.current_focus()}")
    return 0


def cmd_pick(a) -> int:
    """Crosshair a normalized point on a still so a tap coordinate can be eyeballed."""
    s = Session(a.session)
    src = Path(a.image) if Path(a.image).is_absolute() else s.stills / a.image
    if not src.exists():
        print(f"no such still: {src}", file=sys.stderr)
        return 2
    W, H = ff.dims(src)
    pts = []
    for spec in a.at:
        x, y = (float(v) for v in spec.split(","))
        px, py = int(x * W if x <= 1 else x), int(y * H if y <= 1 else y)
        w, h = max(8, int(W * 0.012)), max(8, int(H * 0.006))
        # centre the marker on the point, or the crosshair reads as a half-marker miss
        pts.append({"x": px - w // 2, "y": py - h // 2, "w": w, "h": h})
    out = s.annotated / f"pick_{src.stem}.png"
    annotate(src, out, pts, title=" ".join(a.at))
    print(out)
    return 0


def cmd_new(a) -> int:
    root = Path(a.project).expanduser().resolve()
    flow_preview = {"app": flowmap.app_name(root), "package": flowmap.app_id(root)}
    base = Path(a.out).expanduser() if a.out else Path.home() / ".qoder/app-demo-sessions"
    s = Session.create(base, flow_preview["app"])
    s.update_state(project=str(root), app=flow_preview["app"],
                   package=flow_preview["package"], device_model=None)
    print(s.root)
    return 0


def cmd_flow(a) -> int:
    s = Session(a.session)
    root = Path(a.project).expanduser().resolve() if a.project else Path(s.state()["project"])
    flow = flowmap.build(root)
    s.write("flow", flow)
    s.path("flow").with_name("FLOW_MAP.md").write_text(flowmap.markdown(flow))
    print(f"{len(flow['routes'])} routes, {len(flow['modules'])} modules, "
          f"order: {', '.join(flow['demo_order'][:12])}")
    print(s.path("flow").with_name("FLOW_MAP.md"))
    return 0


def cmd_scaffold(a) -> int:
    """Walk skeleton: one chapter per module in the flow map's suggested order."""
    s = Session(a.session)
    flow = s.read("flow")
    st = s.state()
    steps = [{"chapter": None, "label": "Cold start", "screen": "launch",
              "do": [{"op": "key", "code": 3}, {"op": "wait", "s": 3.5}]}]
    for m in (a.modules or flow["demo_order"]):
        routes = [r["route"] for r in flow["routes"] if r["module"] == m and r["route"]]
        steps.append({"chapter": m, "label": f"{m} - entry",
                      "screen": slugify(m), "do": [{"op": "wait", "s": 1.5},
                                                   {"op": "screenshot", "name": slugify(m)}]})
        for r in routes[:6]:
            steps.append({"chapter": m, "label": f"-> {r}",
                          "do": [{"op": "tap", "rx": 0.5, "ry": 0.5},
                                 {"op": "wait", "s": 1.2}]})
    steps.append({"chapter": None, "label": "Wrap up", "do": [{"op": "wait", "s": 1}]})
    walk = {"app": st.get("app"), "package": st.get("package"),
            "note": "replace every placeholder tap with the real coordinate",
            "steps": steps}
    s.write("walk", walk)
    print(f"{len(steps)} placeholder steps -> {s.path('walk')}")
    return 0


def cmd_launch(a) -> int:
    s = Session(a.session)
    st = s.state()
    pkg = a.pkg or st.get("package")
    adb = _adb(a)
    if a.apk or a.build:
        apk = Path(a.apk) if a.apk else _flutter_debug_apk(Path(st["project"]))
        ok, log = adb.install(apk)
        if not ok:
            print(f"install failed: {log}", file=sys.stderr)
            return 1
        s.update_state(install_kind=f"debug apk {Path(apk).name}")
    if not pkg:
        print("no package id; pass --pkg or run 'flow' against the project", file=sys.stderr)
        return 2
    if a.clear:
        print(f"pm clear {pkg}: wiping local app state on {adb.device} "
              "(server accounts are untouched; you will need to log in again)")
    component = adb.start_app(pkg, wait_s=a.wait, clear=a.clear)
    _stamp(adb, s)
    s.update_state(package=pkg, launch_component=component)
    print(f"{pkg} -> {component} on {adb.device}")
    return 0


def _flutter_debug_apk(project: Path) -> Path:
    out = project / "build/app/outputs/flutter-apk/app-debug.apk"
    if out.exists():
        return out
    flutter = find_flutter()
    if not flutter:
        raise FileNotFoundError("flutter SDK not found and no app-debug.apk present")
    print("building debug apk (writes only into build/)...", file=sys.stderr)
    subprocess.run([str(flutter), "build", "apk", "--debug"], cwd=project, check=True,
                   timeout=3600)
    if not out.exists():
        raise FileNotFoundError(f"build produced no {out}")
    return out


def cmd_scout(a) -> int:
    """Replay the walk without recording; collects stills and hashes for calibration."""
    s, adb = Session(a.session), _adb(a)
    _stamp(adb, s)
    srv = _watch(a, s, adb)
    walk = s.read("walk")
    evs = T.run(adb, s, walk, recorder=None, settle=a.settle)
    for ev in evs:
        if ev.get("hash"):
            step = walk["steps"][ev["i"]]
            step["verify"] = True
            step["expect_hash"] = ev["hash"]
    s.write("walk", walk)
    _linger(a, srv)
    stills = sorted(p.name for p in s.stills.glob("*.png"))
    print(f"scouted {len(evs)} steps, {len(stills)} stills; "
          f"calibrated {sum(1 for e in evs if e.get('hash'))} of them against this pass")
    return 0


def cmd_take(a) -> int:
    """The recording pass: replay the walk while the screen is captured."""
    s, adb = Session(a.session), _adb(a)
    _stamp(adb, s)
    srv = _watch(a, s, adb)
    walk = s.read("walk")
    rec = Recorder(adb, bit_rate=a.bit_rate, segment_s=a.segment_s)
    rec.log = s.raw / "screenrecord.log"
    events = T.run(adb, s, walk, recorder=rec, settle=a.settle)
    raws = rec.pull(s.raw)
    for seg, path in zip(rec.segments, raws):
        seg["seconds"] = round(ff.duration(path), 3)
    recording = ff.concat(raws, s.path("recording")) if len(raws) > 1 else _move(raws[0], s.path("recording"))
    total = ff.duration(recording)
    T.retimestamp(events, rec.segments)
    scenes = ff.scene_changes(recording, a.scene_threshold)
    fit = T.fit_offset(events, scenes)
    fit.update(T.remap(events, scenes, fit["offset"], total))
    tl = T.build(events, fit["offset"], total)
    tl["video"] = str(recording)
    tl["size"] = list(ff.dims(recording))
    tl["scene_cuts"] = scenes
    tl["align"] = fit
    s.write("timeline", tl)
    s.update_state(segments=rec.segments, duration=round(total, 3),
                   align_offset=fit["offset"], align_cost=fit["cost"])
    print(f"recorded {ff.hms(total)} in {len(raws)} segment(s), "
          f"{len(tl['chapters'])} chapters, timing={fit['method']} "
          f"(offset {fit['offset']:.2f}s, {fit.get('snapped', 0)} transitions snapped "
          f"to cuts, mean residual {fit.get('residual', fit['cost']):.2f}s)")
    _warn_align(fit, tl)
    _linger(a, srv)
    return 0


def _warn_align(fit: dict, tl: dict) -> None:
    res = fit.get("residual", fit["cost"])
    if fit.get("method") == "cut anchored":
        if res > 0.35:
            print(f"ALIGN WEAK: chapter starts sit {res:.2f}s from their scene cuts. "
                  "Scrub each one before shipping.")
        if (tl or {}).get("compression", 1.0) < 0.85:
            print(f"NOTE: the video is {tl['compression'] * 100:.0f}% of wall time "
                  f"({ff.hms(tl['duration'])} of {ff.hms(tl.get('wall', 0))}) - "
                  "screenrecord drops idle frames, so pauses are compressed out. "
                  "Timecodes are video time, which is what a viewer experiences.")
        return
    if res < 0:
        print("ALIGN UNVERIFIED: " + fit.get("why", "") +
              ". Chapter times are the host clock; check them before shipping, or "
              "pass --offset to retimeline.")
    elif fit["pinned"] or res > 0.35:
        print(f"ALIGN WEAK (residual {res:.2f}s"
              + (", search hit its bound" if fit["pinned"] else "") +
              "). Open the video at each chapter start to confirm, then re-run: "
              "adrd retimeline --session DIR --offset SEC")


def _move(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return dst


def cmd_retimeline(a) -> int:
    """Re-run alignment and chapter build without re-recording."""
    s = Session(a.session)
    ev = s.read("events")
    tl_old = s.read("timeline") if s.exists("timeline") else {}
    total = a.duration or ff.duration(s.path("recording"))
    if a.offset is not None:
        fit = {"offset": a.offset, "cost": -1.0, "anchors": 0, "pinned": False,
               "why": "offset set by hand"}
        scenes = tl_old.get("scene_cuts", [])
    else:
        scenes = ff.scene_changes(s.path("recording"), a.scene_threshold)
        fit = T.fit_offset(ev["events"], scenes)
    fit.update(T.remap(ev["events"], scenes, fit["offset"], total))
    tl = T.build(ev["events"], fit["offset"], total)
    tl["video"] = str(s.path("recording"))
    tl["size"] = list(ff.dims(s.path("recording")))
    tl["scene_cuts"] = scenes
    tl["align"] = fit
    s.write("timeline", tl)
    s.update_state(align_offset=fit["offset"], align_cost=fit["cost"])
    print(f"timing={fit['method']} offset {fit['offset']:.2f}s "
          f"snapped {fit.get('snapped', 0)} mean residual "
          f"{fit.get('residual', fit['cost']):.2f}s -> {len(tl['chapters'])} chapters")
    _warn_align(fit, tl)
    return 0


def _walk_index(a, steps: list[dict]) -> int | None:
    if len(a.index) != 1:
        print(f"walk {a.what} --index i", file=sys.stderr)
        return None
    try:
        i = int(a.index[0])
    except ValueError:
        print("index must be a number", file=sys.stderr)
        return None
    if not 0 <= i < len(steps):
        print(f"no step {i} - {len(steps)} in this walk", file=sys.stderr)
        return None
    return i


def cmd_walk(a) -> int:
    """Edit the recorded walk. A screen name decides which seconds a mask box covers and a
    label becomes the burned-in caption, so both live twice - in walk.json for the next
    take, and in events.json for the recording that already happened. Changing one and
    not the other is exactly how a box ends up masking the wrong minute."""
    s = Session(a.session)
    w = s.read("walk")
    steps = w.get("steps", [])
    ev = s.read("events", None) if s.exists("events") else None

    if a.what == "list":
        for i, st in enumerate(steps):
            ops = ",".join(o.get("op", "?") for o in st.get("do", []))
            print(f"{i:2d} {st.get('screen', ''):<14} {ops:<26} {st.get('label', '')}")
        return 0

    i = _walk_index(a, steps)
    if i is None:
        return 2
    touched = False
    if a.what == "set":
        if not (a.screen or a.label):
            print("walk set needs --screen and/or --label", file=sys.stderr)
            return 2
        st = steps[i]
        for key in ("screen", "label"):
            val = getattr(a, key)
            if val:
                st[key] = val
                touched = True
    elif a.what == "rm":
        steps.pop(i)
        touched = True

    if ev is not None:
        if a.what == "rm":
            ev["events"] = [e for e in ev["events"] if e.get("i") != i]
            for n, e in enumerate(ev["events"]):
                e["i"] = n
        else:
            for e in ev["events"]:
                if e.get("i") != i:
                    continue
                for key in ("screen", "label"):
                    val = getattr(a, key)
                    if val:
                        e[key] = val
        s.write("events", ev, quiet=True)
    s.write("walk", w)

    if a.what == "rm":
        print(f"removed step {i}; {len(steps)} steps left")
    else:
        st = steps[i]
        print(f"step {i}: screen={st.get('screen', '')} label={st.get('label', '')}")
    if touched and ev is not None and s.exists("timeline"):
        print("  the recording is already made, so rebuild the windows from it: "
              f"adrd retimeline --session {s.root}")
    return 0


def cmd_brand(a) -> int:
    s = Session(a.session)
    b = s.read("branding", {"entries": []})
    tl = s.read("timeline") if s.exists("timeline") else {"screens": {}}
    screens = list(tl.get("screens", {}))

    if a.what == "list":
        for i, e in enumerate(b["entries"]):
            box = [int(v) for v in e["rect"].values()]
            print(f"{i:2d} {e['scope']:<22} {e.get('kind','brand'):<6} "
                  f"{e['mode']:<9} {box}  {e['label']}")
        return 0
    if a.what == "preset":
        if not a.name:
            print(f"presets: {', '.join(R.PRESETS)}", file=sys.stderr)
            return 2
        added = R.preset_entries(a.name, screens, _ref_size(s, None, a.size),
                                 b["entries"])
        b["entries"] += added
        s.write("branding", b)
        print(f"added {len(added)} preset box(es), "
              f"{len(b['entries'])} box(es) total")
        if "topbar-title" in a.name and not screens:
            print("warn: no screen windows in timeline.json yet - presets bound to "
                  "screens resolve after a take. Run 'brand list' afterwards.")
        return 0
    if a.what == "rm":
        idxs = sorted({int(i) for i in a.index}, reverse=True)
        for i in idxs:
            b["entries"].pop(i)
        s.write("branding", b)
        print(f"removed {len(idxs)} of {len(b['entries'])} left")
        return 0
    if a.what == "set":
        # A box drawn on the right pixels but filed under the wrong screen name is
        # invisible for its whole window and blind for every other one; re-scoping it
        # keeps the geometry that was already correct. The reverse is true too, so
        # either half can be changed on its own.
        if len(a.index) != 1 or not (a.screen or a.rect or a.frac):
            print("brand set --index i [--screen NAME|global] [--rect x,y,w,h]",
                  file=sys.stderr)
            return 2
        i = int(a.index[0])
        if not 0 <= i < len(b["entries"]):
            print(f"no box {i} - {len(b['entries'])} in this session", file=sys.stderr)
            return 2
        e = b["entries"][i]
        if a.screen:
            e["scope"] = "global" if a.screen == "global" else f"screen:{a.screen}"
        if a.rect or a.frac:
            size = (tuple(int(v) for v in a.size.split("x")) if a.size
                    else tuple(e.get("ref_size") or _ref_size(s, None, None)))
            if a.rect:
                rect = ff.Rect.parse(a.rect).clamped(*size)
            else:
                x, y, w, h = (float(v) for v in a.frac.split(","))
                rect = ff.Rect(int(x * size[0]), int(y * size[1]),
                               int(w * size[0]), int(h * size[1])).clamped(*size)
            e["rect"] = rect.as_dict()
            e["ref_size"] = list(size)
            e["frac"] = {"x": rect.x / size[0], "y": rect.y / size[1],
                         "w": rect.w / size[0], "h": rect.h / size[1]}
        s.write("branding", b)
        print(f"{e['label']} -> {e['scope']} {[int(v) for v in e['rect'].values()]}")
        return 0
    if a.what == "add":
        if not (a.rect or a.frac):
            print("need --rect x,y,w,h or --frac x,y,w,h", file=sys.stderr)
            return 2
        size = _ref_size(s, a.screen, a.size)
        if a.rect:
            rect = ff.Rect.parse(a.rect).clamped(*size)
        else:
            x, y, w, h = (float(v) for v in a.frac.split(","))
            rect = ff.Rect(int(x * size[0]), int(y * size[1]),
                           int(w * size[0]), int(h * size[1])).clamped(*size)
        scope = f"screen:{a.screen}" if a.screen else "global"
        b["entries"].append(R.entry(size, scope, rect.as_dict(), a.mode, a.sigma,
                                    a.label, a.note, a.kind))
        s.write("branding", b)
        print(f"{a.label} -> {scope} {rect.css()} of {size[0]}x{size[1]}")
        return 0
    return 2


def _ref_size(s: Session, screen: str | None, explicit: str | None) -> tuple[int, int]:
    if explicit:
        return tuple(int(v) for v in explicit.split("x"))
    if screen:
        p = s.stills / f"{screen}.png"
        if p.exists():
            return ff.dims(p)
    st = s.state()
    if st.get("screen_size"):
        return tuple(st["screen_size"])
    raise SystemExit("cannot size the box: pass --size WxH or a --screen still that exists")


def cmd_sheet(a) -> int:
    """Annotated contact sheet of every calibrated screen - the box-placement artifact."""
    s = Session(a.session)
    b = s.read("branding", {"entries": []})
    by_screen: dict[str, list[dict]] = {}
    for e in b["entries"]:
        if e["scope"].startswith("screen:"):
            by_screen.setdefault(e["scope"].split(":", 1)[1], []).append(_rect_of_now(s, e, a.width))
    items = []
    for p in sorted(s.stills.glob("*.png")):
        if p.stem.startswith(("chk_", "pick_", "shot_")) and not a.all_stills:
            continue
        rects = by_screen.get(p.stem, [])
        if rects:
            out = annotate(p, s.annotated / f"{p.stem}.png", rects, title=p.stem)
            items.append((f"{p.stem} ({len(rects)} boxes)", out))
        else:
            items.append((f"{p.stem} (no box yet)", p))
    if not items:
        print("no stills - run scout first")
        return 2
    sheet = contact_sheet(items, s.path("sheet"), cols=a.cols, cell_h=a.cell_h)
    print(f"{len(items)} screens -> {sheet}")
    return 0


def _rect_of_now(s: Session, e: dict, width: int | None) -> dict:
    size = ff.dims(s.path("recording")) if s.exists("recording") and not width \
        else tuple(e.get("ref_size") or s.state().get("screen_size") or (1080, 2400))
    if width:
        h = int(width * size[1] / size[0])
        size = (width, h)
    return R.rect_of(e, size)


def cmd_render(a) -> int:
    s = Session(a.session)
    srv = _watch(a, s)
    tl = s.read("timeline")
    b = s.read("branding")
    size = ff.dims(s.path("recording"))
    warn: list[str] = []
    blurs = R.expand(b, tl, size, pad=a.pad, warn=warn)
    for w in warn:
        print(f"warn: {w}", file=sys.stderr)
    if not blurs:
        print("refusing to render: nothing is masked. Add brand boxes or --preset.",
              file=sys.stderr)
        return 3
    res = R.render(s, tl, blurs, {
        "position": a.captions, "step_captions": a.step_captions,
        "clock": a.clock, "progress": not a.no_progress, "crf": a.crf,
        "preset": a.preset, "out": a.out,
    })
    res["masks"] = len(blurs)
    res["unmatched_scopes"] = warn
    real = ff.duration(Path(res["video"]))
    res["rendered_seconds"] = round(real, 3)
    if tl["duration"] - real > 0.5:
        # screenrecord's container runs longer than the frames it holds, so the last
        # chapter would otherwise cite a timecode past the end of the file
        for c in tl["chapters"]:
            c["t1"] = min(c["t1"], real)
        tl["duration"] = round(real, 3)
        s.write("timeline", tl)
        Path(res["vtt"]).write_text(ff.webvtt(tl["chapters"]))
        print(f"note: the finished file runs {real:.2f}s, shorter than the timeline "
              "claimed; chapter ends and the chapter list now match the player")
    res["proof"] = R.proof(s, tl, blurs, Path(res["video"]))
    s.write("render", res)
    pr = res["proof"]
    print(f"{res['video']}")
    print(f"{res['vtt']}")
    print(f"{pr['sheet']}")
    print(f"{len(blurs)} mask(s), {res['filters']} filters, {res['chapters']} chapters, "
          f"{pr['regions']} regions cut from the output")
    print("region check: " + (", ".join(f"{v} {k}" for k, v in pr["tally"].items())
                              or "no regions"))
    bad = [r for r in pr["attention"] if r["state"].startswith("unchanged")]
    if bad:
        print(f"BOX WRONG: {len(bad)} region(s) changed nothing - "
              f"{sorted({(r['chapter'], r['label']) for r in bad})[:6]}")
    for k, v in pr["tally"].items():
        if k.startswith("weakly") or k.startswith("flat"):
            print(f"NOTE {v}x {k}")
    print(f"every region sample needs an eye on {pr['sheet']} - the numeric check "
          f"saturates and cannot certify readability")
    if pr["chapters_without_blur"]:
        print(f"NOTE: no mask lands in {', '.join(pr['chapters_without_blur'])}")
    for w in warn:
        print(f"WARN {w}")
    print("open the sweep sheet before sharing: adrd sweep --session "
          f"{a.session} then read render/sweep.png")
    _linger(a, srv)
    return 0


def cmd_sweep(a) -> int:
    """Full frames from the finished video, one sheet per pass - the only check that can
    see branding sitting outside the boxes you declared."""
    s = Session(a.session)
    tl = s.read("timeline")
    rend = s.read("render", {})
    video = Path(a.video) if a.video else Path(rend.get("video") or s.path("final"))
    if not video.exists():
        print("nothing rendered yet", file=sys.stderr)
        return 2
    items = []
    for c in tl["chapters"]:
        span = c["t1"] - c["t0"]
        for k, t in enumerate([c["t0"] + min(1.0, span * 0.4),
                               c["t0"] + span * 0.75][:a.frames]):
            f = ff.frame(video, t, s.render / "sweep_frames" / f"c{c['index']:02d}_{k}.png",
                         height=a.height)
            items.append((f"{c['index']}. {c['module']}  {ff.tc(t)}", f))
    sheet = contact_sheet(items, s.render / "sweep.png", cols=a.cols, cell_h=a.height)
    s.update_state(sweep=str(sheet))
    print(f"{len(items)} full frames -> {sheet}")
    print("Read this sheet top to bottom: any client name, logo, store screenshot, "
          "package id or avatar still visible is a leak this tool cannot detect on its own.")
    return 0


def cmd_proof(a) -> int:
    """Re-run the leak check on an existing render without re-encoding."""
    s = Session(a.session)
    tl = s.read("timeline")
    size = ff.dims(s.path("recording"))
    warn: list[str] = []
    blurs = R.expand(s.read("branding"), tl, size, pad=a.pad, warn=warn)
    video = Path(a.video) if a.video else Path(s.read("render", {}).get("video")
                                               or s.path("final"))
    pr = R.proof(s, tl, blurs, video)
    s.update_state(proof=pr["tally"], needs_attention=len(pr["attention"]))
    rend = s.read("render", {})
    rend["proof"] = pr
    s.write("render", rend)
    print(f"{video} -> {pr['regions']} region checks")
    print(", ".join(f"{v} {k}" for k, v in pr["tally"].items()) or "none")
    print(pr["sheet"])
    for w in warn:
        print(f"WARN {w}")
    return 1 if any(r["state"].startswith("unchanged") for r in pr["attention"]) else 0


def cmd_export(a) -> int:
    """Bundle both videos, every evidence image, the brief, and a hashed manifest."""
    s = Session(a.session)
    if not s.path("final").exists():
        print("nothing rendered yet - run 'adrd render' first", file=sys.stderr)
        return 2
    if not a.no_raw and not a.yes:
        print("the bundle will include the UNMASKED capture (recording_UNMASKED.mp4). "
              "Pass --no-raw to leave it out, or --yes to include it knowingly.")
        return 3
    zip_path = X.build(s, brief_text=a.brief or "", brief_file=a.brief_file,
                       include_raw=not a.no_raw)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    print(zip_path)
    print(f"{len(names)} files, {zip_path.stat().st_size / 1e6:.1f} MB")
    for n in names:
        if n.count("/") <= 1 and not n.startswith("evidences/"):
            print(f"  {n}")
    ev = [n for n in names if n.startswith("evidences/")]
    print(f"  evidences/... {len(ev)} images")
    if not a.no_raw:
        print("  contains the unmasked capture - labelled READ_ME_FIRST.txt inside")
    return 0


def cmd_tunnel(a) -> int:
    """One long-lived tunnel so the public hostname survives dashboard restarts."""
    s = Session(a.session)
    d = s.root.parent
    if getattr(a, "login", False):
        if not shutil.which("cloudflared"):
            print("cloudflared is not installed", file=sys.stderr)
            return 1
        if W.cf_cert().exists():
            print(f"already signed in: {W.cf_cert()}")
            return 0
        print("cloudflared opens a browser here. Choose the zone on the Cloudflare page, "
              "then Authorize, and leave this command running - the callback has nowhere "
              "to land once it has exited.", flush=True)
        rc = subprocess.call(["cloudflared", "tunnel", "login"])
        if W.cf_cert().exists():
            print(f"signed in: {W.cf_cert()}\nnext: adrd watch --session DIR --public "
                  "--hostname adrd.<your-domain>")
            return 0
        print(f"no certificate at {W.cf_cert()} (cloudflared exit {rc}).\n"
              "  - the browser has to finish while this command is still waiting\n"
              "  - the account needs a domain (zone) on Cloudflare; an account without one\n"
              "    cannot be handed a fixed hostname, only throwaway quick-tunnel names\n"
              "  - if the browser downloaded cert.pem instead, move it to that path",
              file=sys.stderr)
        return 1
    if a.stop:
        rec = W.read_json(W.tunnel_record(d), {})
        if rec.get("pid"):
            try:
                os.kill(int(rec["pid"]), 15)
            except OSError:
                pass
        W.tunnel_record(d).unlink(missing_ok=True)
        print("tunnel stopped")
        return 0
    live = W.tunnel_status(d)
    if live and not a.restart and not a.hostname:
        print(live)
        return 0
    port = a.port or int((W.read_json(W.tunnel_record(d), {}) or {}).get("local", "x:0").rsplit(":", 1)[-1]) or 8734
    url = W.tunnel(f"http://127.0.0.1:{port}", d / "tunnel.log", d, hostname=a.hostname)
    print(url or "tunnel did not come up")
    return 0 if url else 1


def cmd_doc(a) -> int:
    s = Session(a.session)
    tl = s.read("timeline")
    walk = s.read("walk")
    b = s.read("branding", {"entries": []})
    flow = s.read("flow", None) if s.exists("flow") else None
    rend = s.read("render", {})
    pr = rend.get("proof")
    st = s.state()
    device = {"model": st.get("device_model"), "serial": st.get("serial"),
              "size": st.get("screen_size")}
    st2 = s.state()
    # Every word of the app name and package is a brand token, so "union" is folded out
    # of "UNION SCORE" without anyone having to enumerate the product's phrases.
    app = st2.get("app", "") or ""
    pkg = st2.get("package", "") or ""
    words = [w for w in re.split(r"[._\-]+", app) if len(w) > 2]
    words += [w for w in pkg.split(".") if len(w) > 2 and w not in ("com", "app")]
    tokens = list(a.redact or []) + words + [app, pkg, app.replace("_", " ")]
    text = docgen.markdown(s, tl, walk, b, flow, pr,
                           {"cost": st.get("align_cost", -1),
                            "samples": len(tl.get("scene_cuts", []))}, device,
                           redact=tokens)
    p = s.path("doc")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    try:
        report = docgen.pdf(text, f"Walkthrough - {st.get('app', 'app')}", s.path("report"))
    except docgen.MissingTool as e:
        report = f"skipped: {e}"
    extra = docgen.artifacts(s, {"FLOW_MAP.md": str(s.path("flow").with_name("FLOW_MAP.md"))})
    print(p)
    print(report)
    print(extra)
    return 0


def cmd_concat(a) -> int:
    s = Session(a.session)
    raws = sorted(s.raw.glob("raw*.mp4"))
    out = ff.concat(raws, s.path("recording")) if len(raws) > 1 else _move(raws[0], s.path("recording"))
    print(f"{out} {ff.hms(ff.duration(out))}")
    return 0


WATCH_HELP = "serve the dashboard on this machine while the command runs"


def watch_flags(sp) -> None:
    sp.add_argument("--serial", help="adb serial (accepted here as well as before the verb)")
    sp.add_argument("--watch", action="store_true", help=WATCH_HELP)
    sp.add_argument("--port", type=int, default=8734,
                    help="fixed so the URL stays the same; walks up if busy")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--lan", action="store_true",
                    help="bind 0.0.0.0 and require a token (unmasked screens leave this Mac)")
    sp.add_argument("--token", help="access token; generated when --lan is used without one")
    sp.add_argument("--keep-alive", type=int, default=60,
                    help="seconds the dashboard stays up after the command ends")
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--hide-unmasked", action="store_true",
                    help="refuse to serve the unmasked raw capture to viewers")
    sp.add_argument("--public", action="store_true",
                    help="also expose it through a cloudflared quick tunnel (token forced)")
    sp.add_argument("--expires", type=float, default=6.0,
                    help="hours until a --public dashboard closes itself")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adrd", description=__doc__)
    p.add_argument("--serial", help="adb serial; defaults to the emulator, then first device")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor")
    d.set_defaults(fn=cmd_doctor)
    d = sub.add_parser("devices")
    d.set_defaults(fn=cmd_devices)

    u = sub.add_parser("url", help="print the live public link for a session")
    u.add_argument("--session", required=True)
    u.set_defaults(fn=cmd_url)

    n = sub.add_parser("new", help="create a session dir from a Flutter project path")
    n.add_argument("--project", required=True)
    n.add_argument("--out", help="where sessions live")
    n.set_defaults(fn=cmd_new)

    f = sub.add_parser("flow", help="read routes and modules out of the source")
    f.add_argument("--session", required=True)
    f.add_argument("--project")
    f.set_defaults(fn=cmd_flow)

    sc = sub.add_parser("scaffold", help="walk skeleton with one chapter per module")
    sc.add_argument("--session", required=True)
    sc.add_argument("--modules", nargs="*")
    sc.set_defaults(fn=cmd_scaffold)

    l = sub.add_parser("launch", help="install and cold-start the app")
    l.add_argument("--session", required=True)
    l.add_argument("--pkg")
    l.add_argument("--apk")
    l.add_argument("--build", action="store_true", help="flutter build apk --debug if needed")
    l.add_argument("--wait", type=float, default=4.0)
    l.add_argument("--clear", action="store_true",
                   help="pm clear first: wipes local storage so the app starts logged out")
    l.set_defaults(fn=cmd_launch)

    sh = sub.add_parser("shot", help="one still, with size and focus printed")
    sh.add_argument("--session", required=True)
    sh.add_argument("--name")
    sh.set_defaults(fn=cmd_shot)

    pk = sub.add_parser("pick", help="crosshair a candidate tap point on a still")
    pk.add_argument("--session", required=True)
    pk.add_argument("--image", required=True)
    pk.add_argument("--at", nargs="+", required=True, metavar="RX,RY")
    pk.set_defaults(fn=cmd_pick)

    st = sub.add_parser("scout", help="replay the walk with no recording; collect stills")
    st.add_argument("--session", required=True)
    st.add_argument("--settle", type=float, default=1.0)
    watch_flags(st)
    st.set_defaults(fn=cmd_scout)

    t = sub.add_parser("take", help="record while replaying the walk")
    t.add_argument("--session", required=True)
    t.add_argument("--bit-rate", type=int, default=12_000_000)
    t.add_argument("--segment-s", type=int, default=1740)
    t.add_argument("--settle", type=float, default=0.9)
    t.add_argument("--scene-threshold", type=float, default=0.4)
    watch_flags(t)
    t.set_defaults(fn=cmd_take)

    rt = sub.add_parser("retimeline", help="re-fit timing without re-recording")
    rt.add_argument("--session", required=True)
    rt.add_argument("--offset", type=float)
    rt.add_argument("--duration", type=float)
    rt.add_argument("--scene-threshold", type=float, default=0.4)
    rt.set_defaults(fn=cmd_retimeline)

    cc = sub.add_parser("concat", help="join pulled segments")
    cc.add_argument("--session", required=True)
    cc.set_defaults(fn=cmd_concat)

    wk = sub.add_parser("walk", help="list or edit the recorded walk")
    wk.add_argument("what", choices=["list", "set", "rm"])
    wk.add_argument("--session", required=True)
    wk.add_argument("--index", nargs="*", default=[])
    wk.add_argument("--screen", help="which screen this step is meant to land on")
    wk.add_argument("--label", help="caption burned in for this step")
    wk.set_defaults(fn=cmd_walk)

    b = sub.add_parser("brand", help="add/list brand boxes")
    b.add_argument("what", choices=["add", "list", "rm", "set", "preset"])
    b.add_argument("--session", required=True)
    b.add_argument("--screen", help="screen still name this box belongs to")
    b.add_argument("--rect", help="x,y,w,h in the still's pixels")
    b.add_argument("--frac", help="x,y,w,h as 0..1 of the screen")
    b.add_argument("--label", default="brand mark")
    b.add_argument("--note", default="")
    b.add_argument("--mode", choices=["blur", "pixelate", "solid"], default="blur")
    b.add_argument("--kind", choices=["brand", "pii"], default="brand",
                   help="brand = app name/logo, full blur; pii = email/mobile/password/name, 0.4")
    b.add_argument("--sigma", type=float, default=0,
                   help="0 sizes the blur to the box; only set this to override")
    b.add_argument("--size", help="WxH reference for --rect when no still exists")
    b.add_argument("--name", nargs="*", choices=list(R.PRESETS),
                   help="preset boxes; use with 'preset'")
    b.add_argument("--index", nargs="*", default=[])
    b.set_defaults(fn=cmd_brand)

    g = sub.add_parser("sheet", help="annotated contact sheet of screens")
    g.add_argument("--session", required=True)
    g.add_argument("--cols", type=int, default=3)
    g.add_argument("--cell-h", type=int, default=460)
    g.add_argument("--width", type=int)
    g.add_argument("--all-stills", action="store_true")
    g.set_defaults(fn=cmd_sheet)

    r = sub.add_parser("render", help="mask branding, burn captions, add chapters")
    r.add_argument("--session", required=True)
    r.add_argument("--out")
    r.add_argument("--captions", choices=["bottom", "top"], default="bottom")
    r.add_argument("--step-captions", action="store_true")
    r.add_argument("--clock", action="store_true")
    r.add_argument("--no-progress", action="store_true")
    r.add_argument("--pad", type=float, default=0.35)
    r.add_argument("--crf", type=int, default=22)
    r.add_argument("--preset", default="veryfast")
    watch_flags(r)
    r.set_defaults(fn=cmd_render)

    sw = sub.add_parser("sweep", help="full-frame sheet of the render for human review")
    sw.add_argument("--session", required=True)
    sw.add_argument("--video")
    sw.add_argument("--frames", type=int, default=2)
    sw.add_argument("--height", type=int, default=520)
    sw.add_argument("--cols", type=int, default=3)
    sw.set_defaults(fn=cmd_sweep)

    pf = sub.add_parser("proof", help="re-check the masked regions in the rendered file")
    pf.add_argument("--session", required=True)
    pf.add_argument("--video")
    pf.add_argument("--pad", type=float, default=0.35)
    pf.set_defaults(fn=cmd_proof)

    w = sub.add_parser("watch", help="serve the dashboard: live device view + task state")
    w.add_argument("--session", required=True)
    w.add_argument("--serial", help="adb serial to stream from")
    w.add_argument("--device", action="store_true", help="also stream live screencaps")
    w.add_argument("--port", type=int, default=8734)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--lan", action="store_true",
                   help="bind 0.0.0.0; a token is then generated and required")
    w.add_argument("--token")
    w.add_argument("--hostname",
                   help="fixed public host for --public; needs `adrd tunnel --login` once")
    w.add_argument("--public", action="store_true",
                   help="expose through a cloudflared quick tunnel; token is forced on")
    w.add_argument("--expires", type=float, default=6.0)
    w.add_argument("--no-browser", action="store_true")
    w.add_argument("--hide-unmasked", action="store_true",
                   help="refuse to serve the unmasked raw capture to viewers")
    w.add_argument("--daemon", action="store_true",
                   help="survive the shell that started it (new session, not just nohup)")
    w.set_defaults(fn=cmd_watch)

    ex = sub.add_parser("export", help="final bundle: videos + evidences/ + brief + manifest")
    ex.add_argument("--session", required=True)
    ex.add_argument("--brief", help="the instructions this run was performed against")
    ex.add_argument("--brief-file", help="read the brief from a file instead")
    ex.add_argument("--no-raw", action="store_true", help="exclude the unmasked capture")
    ex.add_argument("--yes", action="store_true", help="include the unmasked capture knowingly")
    ex.add_argument("--dest", help="bundle folder; defaults to <session>/export")
    ex.set_defaults(fn=cmd_export)

    tn = sub.add_parser("tunnel", help="show or start the persistent public hostname")
    tn.add_argument("--session", required=True)
    tn.add_argument("--stop", action="store_true")
    tn.add_argument("--restart", action="store_true")
    tn.add_argument("--login", action="store_true",
                    help="one-time Cloudflare browser sign-in that makes --hostname usable")
    tn.add_argument("--hostname",
                    help="fixed public host, e.g. live.example.com; needs `tunnel --login`")
    tn.add_argument("--port", type=int)
    tn.set_defaults(fn=cmd_tunnel)

    dc = sub.add_parser("doc", help="timestamped write-up + artifact index")
    dc.add_argument("--session", required=True)
    dc.add_argument("--redact", action="append",
                    help="extra token to fold out of the shareable doc (repeatable)")
    dc.set_defaults(fn=cmd_doc)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
