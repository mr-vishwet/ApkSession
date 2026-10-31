"""Walk replay, chapter assembly and host-clock -> video-clock alignment."""
from __future__ import annotations

import time

from .img import ahash, differs
from .watch import write_status

ACTUATING = {"tap", "long_press", "swipe", "key", "text", "back", "hide_keyboard"}


def _scale(op: dict, size: tuple[int, int]) -> tuple[int, int]:
    """Walks store normalized coordinates so one script works on phone and emulator."""
    W, H = size
    if op.get("rx") is not None and op.get("ry") is not None:
        return int(op["rx"] * W), int(op["ry"] * H)
    return int(op.get("x", 0)), int(op.get("y", 0))


def _pair(op: dict, size: tuple[int, int], a: str, b: str) -> tuple[int, int]:
    W, H = size
    if op.get(f"r{a}") is not None and op.get(f"r{b}") is not None:
        return int(op[f"r{a}"] * W), int(op[f"r{b}"] * H)
    return int(op.get(a, 0)), int(op.get(b, 0))


def _apply(adb, op: dict, size, session, event: dict) -> None:
    kind = op["op"]
    if kind == "wait":
        time.sleep(float(op.get("s", 1.0)))
    elif kind == "tap":
        x, y = _scale(op, size)
        adb.tap(x, y)
        event["at"] = [x, y]
    elif kind == "long_press":
        x, y = _scale(op, size)
        adb.long_press(x, y, int(op.get("ms", 800)))
        event["at"] = [x, y]
    elif kind == "swipe":
        x1, y1 = _pair(op, size, "x", "y")
        x2, y2 = _pair(op, size, "x2", "y2")
        adb.swipe(x1, y1, x2, y2, int(op.get("ms", 260)))
        event["from"], event["to"] = [x1, y1], [x2, y2]
    elif kind == "key":
        adb.keyevent(op.get("code", 4))
    elif kind == "back":
        adb.keyevent(4)
    elif kind == "hide_keyboard":
        adb.keyevent(111)
    elif kind == "text":
        adb.type_text(str(op.get("value", "")))
        event["typed"] = "<redacted>" if op.get("secret") else str(op.get("value", ""))
    elif kind == "screenshot":
        name = op.get("name") or f"shot_{event['i']:03d}"
        p = session.stills / f"{_safe(name)}.png"
        adb.screencap(p)
        event["still"] = str(p)
    else:
        raise ValueError(f"unknown op {kind!r} in step {event.get('i')}")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))


def video_time(host: float, segments: list[dict], now=None) -> float:
    """Seconds into the concatenated video for a host clock reading."""
    acc = 0.0
    for seg in segments:
        start = seg["host_start"]
        end = seg.get("host_end")
        if end is None:
            return acc + max(0.0, host - start)
        if host < end:
            return acc + max(0.0, host - start)
        acc += seg.get("seconds") or (end - start)
    return acc


def run(adb, session, walk: dict, recorder=None, size=None,
        settle: float = 0.8) -> list[dict]:
    """Replay the walk; with a recorder it becomes the take, without it a scout pass."""
    size = size or adb.screen_size()
    events: list[dict] = []
    base = None
    if recorder:
        recorder.start()
        base = recorder.segments[0]["host_start"]
    base = base or time.time()
    session.update_state(rec_start=base if recorder else None, scout=not recorder,
                         screen_size=list(size),
                         segments=[dict(s) for s in (recorder.segments if recorder else [])])

    steps = walk.get("steps", [])
    for i, step in enumerate(steps):
        now = time.time()
        ev = {"i": i, "chapter": step.get("chapter"), "label": step.get("label", ""),
              "screen": step.get("screen", ""), "host": now,
              "t_raw": video_time(now, recorder.segments) if recorder else now - base,
              "ops": [o["op"] for o in step.get("do", [])]}
        events.append(ev)
        write_status(session.root, stage="recording" if recorder else "scouting",
                     i=i, total=len(steps), chapter=ev["chapter"], label=ev["label"],
                     screen=ev["screen"], t=round(ev["t_raw"], 2))
        print(f"[{i:03d}] t={ev['t_raw']:7.2f} {ev['chapter'] or '-':<14} {ev['label'][:58]}")
        for op in step.get("do", []):
            _apply(adb, op, size, session, ev)
        if step.get("verify"):
            p = session.stills / f"chk_{i:03d}.png"
            adb.screencap(p)
            h = ahash(p)
            ev["hash"] = hex(h)
            ev["mismatch"] = bool(step.get("expect_hash")) and differs(
                int(step["expect_hash"], 16), h)
        time.sleep(settle)

    write_status(session.root, stage="finalising", i=len(steps), total=len(steps))
    if recorder:
        recorder.stop()
        segs = recorder.segments
    else:
        segs = []
    for ev in events:
        ev["t_raw"] = round(video_time(ev["host"], segs), 3) if segs else round(ev["t_raw"], 3)
    session.write("events", {"size": list(size), "segments": segs, "events": events},
                  quiet=True)
    return events


def retimestamp(events: list[dict], segments: list[dict]) -> list[dict]:
    """Re-derive video times once each segment's true duration is known."""
    for e in events:
        e["t_raw"] = round(video_time(e["host"], segments), 3)
    return events


def transition_times(events: list[dict]) -> list[float]:
    """Host times where the UI should actually change: a new chapter or a new screen."""
    out, prev_ch, prev_scr = [], None, None
    for e in events:
        ch, scr = e.get("chapter"), e.get("screen")
        if (ch and ch != prev_ch) or (scr and scr != prev_scr):
            out.append(e["t_raw"])
        prev_ch, prev_scr = ch or prev_ch, scr or prev_scr
    return out


def fit_offset(events: list[dict], scenes: list[float], span: tuple[float, float] = (0.0, 3.0),
               step: float = 0.01) -> dict:
    """Constant host->video shift that lines our transitions up with the video's own cuts.

    Only chapter/screen changes anchor the fit - a tap on a static screen makes no cut,
    and median cost keeps keyboard animations from dragging the answer.
    """
    raws = transition_times(events) or [e["t_raw"] for e in events
                                         if set(e.get("ops", [])) & ACTUATING]
    raws = raws or [e["t_raw"] for e in events]
    if not raws or not scenes:
        return {"offset": 0.0, "cost": -1.0, "anchors": len(raws), "pinned": False,
                "why": "no scene cuts found in the video, or no anchored steps"}
    best, best_cost, d = span[0], float("inf"), span[0]
    while d <= span[1] + 1e-9:
        ds = sorted(min(1.5, min(abs(r - d - s) for s in scenes)) for r in raws)
        cost = ds[len(ds) // 2]
        if cost < best_cost - 1e-9:
            best_cost, best = cost, d
        d = round(d + step, 4)
    return {"offset": round(best, 3), "cost": round(best_cost, 3), "anchors": len(raws),
            "pinned": best <= span[0] + 1e-6 or best >= span[1] - 1e-6,
            "why": "" if best_cost < 0.35 else "median residual above 0.35s"}


def chapters(events: list[dict], total: float, offset: float,
             key: str = "t_raw") -> list[dict]:
    """Contiguous runs sharing a chapter label become the video's chapters."""
    marks = []
    for e in events:
        if not e.get("chapter"):
            continue
        t = round(max(0.0, e[key] - offset), 3)
        if marks and marks[-1][0] == e["chapter"]:
            continue
        if marks and t <= marks[-1][1]:
            continue
        marks.append((e["chapter"], t, e.get("label", "")))
    marks.sort(key=lambda m: m[1])
    out = []
    for i, (name, t, label) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else round(total, 3)
        out.append({"index": i + 1, "title": f"{i + 1}. {name}", "module": name,
                    "t0": round(t, 3), "t1": round(max(t + 0.5, end), 3),
                    "opened_with": label})
    return out


def screen_spans(events: list[dict], offset: float, total: float,
                 key: str = "t_raw") -> dict[str, list[list[float]]]:
    """Time windows each named screen occupies; brand rects bind to these."""
    spans: dict[str, list[list[float]]] = {}
    for i, e in enumerate(events):
        name = e.get("screen")
        if not name:
            continue
        t0 = round(max(0.0, e[key] - offset), 3)
        nxt = [x for x in events[i + 1:] if x.get("screen") != name]
        t1 = round(max(0.0, nxt[0][key] - offset), 3) if nxt else round(total, 3)
        spans.setdefault(name, []).append([t0, max(t1, t0 + 0.5)])
    for name, windows in spans.items():
        merged: list[list[float]] = []
        for a, b in sorted(windows):
            if merged and a <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        spans[name] = merged
    return spans


def anchor_events(events: list[dict]) -> list[dict]:
    """The steps that should be visible as a transition: a new chapter or a new screen."""
    out, prev_ch, prev_scr = [], None, None
    for e in events:
        ch, scr = e.get("chapter"), e.get("screen")
        if (ch and ch != prev_ch) or (scr and scr != prev_scr):
            out.append(e)
        prev_ch, prev_scr = ch or prev_ch, scr or prev_scr
    return out


def _match_anchors(want: list[float], cuts: list[float], giveup: float = 1.5):
    """Order-preserving match of our transitions onto the video's cuts.

    Extra cuts are cheap to ignore (animations, keyboards); giving up on an anchor costs,
    because a step that changed nothing visually still needs a time.
    """
    n, m = len(want), len(cuts)
    INF = float("inf")
    f = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        f[0][j] = 0.02 * j
        back[0][j] = "skipcut" if j else None
    for i in range(1, n + 1):
        f[i][0] = giveup * i
        back[i][0] = "giveup"
        for j in range(1, m + 1):
            best, how = f[i][j - 1] + 0.02, "skipcut"
            g = f[i - 1][j] + giveup
            if g < best:
                best, how = g, "giveup"
            mt = f[i - 1][j - 1] + abs(want[i - 1] - cuts[j - 1])
            if mt < best:
                best, how = mt, "match"
            f[i][j], back[i][j] = best, how
    pairs, i, j = [], n, m
    while i > 0:
        how = back[i][j]
        if how == "match":
            pairs.append((i - 1, cuts[j - 1]))
            i, j = i - 1, j - 1
        elif how == "skipcut":
            j -= 1
        else:
            i -= 1
    return list(reversed(pairs)), f[n][m]


def _interp(x: float, pts: list[tuple[float, float]]) -> float:
    if not pts:
        return x
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def measured_scale(total: float, host: list[float]) -> float:
    """How much the encoder dropped, measured rather than fitted.

    screenrecord can only remove idle time, never invent it, so the ratio of video
    length to wall length is the scale - and it is bounded at 1.0. Regressing the
    anchor pairs instead gave a confident 0.05 on a take that barely compressed at all.
    """
    if len(host) < 2 or not total:
        return 1.0
    wall = host[-1] - host[0]
    if wall <= 5:
        return 1.0
    return min(1.0, max(0.05, round(total / wall, 3)))


def remap(events: list[dict], scenes: list[float], offset: float,
          total: float = 0.0) -> dict:
    """Rewrite every event onto the video's own timeline.

    Android's screenrecord encodes on surface invalidation, not on a clock: measured on
    an emulator, 5 wall seconds on a static screen produced a 0.00s file and 5 seconds of
    swiping produced 3.30s. Idle time is simply not in the video, so host time runs ahead
    of video time by however long the walkthrough paused, and no single constant offset
    describes that. Anchoring each transition to the cut that *is* that transition fixes
    it for any pause length; everything between anchors is interpolated.
    """
    anchors = anchor_events(events)
    if not anchors or not scenes:
        return {"method": "host clock", "snapped": 0, "residual": -1.0,
                "scale": scale, "note": "no scene cuts or no transition steps to anchor on"}
    host = [a["t_raw"] for a in anchors]
    scale = measured_scale(total, [e["t_raw"] for e in events])
    if not total:
        return {"method": "host clock", "snapped": 0, "residual": -1.0, "scale": 1.0,
                "note": "remap called without the video duration - caller bug, not a "
                        "data problem. Refusing to guess a scale."}
    want = [(h - host[0]) * scale for h in host]
    gaps = sorted(want[i + 1] - want[i] for i in range(len(want) - 1)) or [2.0]
    giveup = max(1.0, gaps[len(gaps) // 2] * 0.9)
    pairs, cost = _match_anchors(want, scenes, giveup)
    if len(pairs) < 2:
        return {"method": "constant offset", "snapped": len(pairs), "scale": scale,
                "residual": round(cost, 3),
                "note": "too few cuts matched to build a piecewise map"}
    # Control points are (host time, video time). The recording opens on the first step,
    # so that is the origin - the screen you start on produces no cut to anchor to.
    pts = [(events[0]["t_raw"], 0.0)] + [(anchors[i]["t_raw"], cut) for i, cut in pairs]
    pts = sorted({(max(0.0, a), max(0.0, b)) for a, b in pts})
    for e in events:
        e["t_video"] = round(_interp(e["t_raw"], pts), 3)
    resid = [abs(want[i] - cut) for i, cut in pairs]
    return {"method": "cut anchored", "snapped": len(pairs), "anchors": len(anchors),
            "scale": scale,
            "residual": round(sum(resid) / len(resid), 3),
            "max_residual": round(max(resid), 3)}


def build(events: list[dict], offset: float, total: float) -> dict:
    """Chapter and screen windows, read off t_video when remap() supplied one."""
    for e in events:
        e.setdefault("t_video", max(0.0, e["t_raw"] - offset))
    # The honest compression figure is the measured ratio, not the regression slope:
    # with few anchors the slope can read 0.05 on a take that barely compressed at all.
    wall = (events[-1]["t_raw"] - events[0]["t_raw"]) if len(events) > 1 else 0.0
    return {
        "duration": round(total, 3), "wall": round(wall, 3),
        "compression": round(total / wall, 3) if wall > 5 else 1.0,
        "offset": offset,
        "chapters": chapters(events, total, 0.0, key="t_video"),
        "events": [{**e, "t": round(max(0.0, e["t_video"]), 3)} for e in events],
        "screens": screen_spans(events, 0.0, total, key="t_video"),
    }
