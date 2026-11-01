"""Turn branding + timeline into one filtergraph, render, and prove nothing leaked."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import ff
from .img import contact_sheet, crop_regions, mask_state
from .watch import write_status

# The Flutter MODEL_MARKER banner drawn by debug/profile builds is a dead giveaway
# that this is not a store build, and it sits over the app bar in every screen.
PII_STRENGTH = 0.4  # personal data gets a light blur, brand gets a full one


PRESETS = {
    "debug-banner": {
        "label": "Flutter DEBUG/PROFILE banner",
        "scope": "global",
        "frac": {"x": 0.0, "y": 0.0, "w": 0.30, "h": 0.115},
        "mode": "blur", "sigma": 0,
    },
    "topbar-title": {
        "label": "app bar title / wordmark",
        "scope": "screen",
        "frac": {"x": 0.16, "y": 0.032, "w": 0.55, "h": 0.036},
        "mode": "blur", "sigma": 0,
    },
    "status-bar": {
        "label": "status bar (carrier, battery, clock)",
        "scope": "global",
        "frac": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.032},
        "mode": "pixelate", "sigma": 0,
    },
}


def preset_entries(names: list[str], screens: list[str], size: tuple[int, int]) -> list[dict]:
    """Preset fracs are resolved against `size` so they behave like hand-placed boxes."""
    out = []
    W, H = size
    for n in names:
        pr = PRESETS[n]
        px = {"x": int(pr["frac"]["x"] * W), "y": int(pr["frac"]["y"] * H),
              "w": int(pr["frac"]["w"] * W), "h": int(pr["frac"]["h"] * H)}
        scopes = [f"screen:{s}" for s in screens] if pr["scope"] == "screen" else ["global"]
        for sc in scopes or ["global"]:
            e = entry(size, sc, px, pr["mode"], pr["sigma"], pr["label"],
                      f"preset {n}")
            e["preset"] = n
            out.append(e)
    return out


def entry(size: tuple[int, int], scope: str, rect_px: dict, mode: str,
          sigma: float, label: str, note: str = "", kind: str = "brand") -> dict:
    """sigma 0 means 'scale the blur to the box'. kind picks how hard it blurs."""
    W, H = size
    return {
        "scope": scope,
        "rect": rect_px,
        "frac": {"x": rect_px["x"] / W, "y": rect_px["y"] / H,
                 "w": rect_px["w"] / W, "h": rect_px["h"] / H},
        "ref_size": [W, H],
        "mode": mode, "sigma": sigma, "label": label or "brand mark", "note": note,
        "kind": kind,
    }


def rect_of(e: dict, size: tuple[int, int]) -> dict:
    """Brand boxes are stored as fractions so an emulator take fits a phone take."""
    frac = e.get("frac")
    if not frac:
        return e["rect"]
    W, H = size
    return {"x": int(frac["x"] * W), "y": int(frac["y"] * H),
            "w": int(frac["w"] * W), "h": int(frac["h"] * H)}


def _windows(scope: str, tl: dict, total: float, pad: float) -> list[tuple[float, float]]:
    if scope == "global":
        return [(0.0, total)]
    name = scope.split(":", 1)[1]
    spans = tl.get("screens", {}).get(name)
    if not spans:
        return []
    return [(max(0.0, a - pad), min(total, b + pad)) for a, b in spans]


def expand(branding: dict, tl: dict, size: tuple[int, int],
           pad: float = 0.35, warn: list[str] | None = None) -> list[dict]:
    """Merge every box that shares a rect+mode into one filter with a summed enable."""
    grouped: dict[tuple, dict] = {}
    flagged: set[str] = set()
    for e in branding.get("entries", []):
        wins = _windows(e["scope"], tl, tl["duration"], pad)
        if not wins:
            msg = (f"no time window for {e['scope']} - screen name missing from the "
                   f"walk, or the take never reached it")
            if warn is not None and msg not in flagged:
                flagged.add(msg)
                warn.append(msg)
            continue
        r = ff.Rect(**rect_of(e, size)).clamped(*size)
        mode = e.get("mode", "blur")
        kind = e.get("kind", "brand")
        strength = PII_STRENGTH if kind == "pii" else 1.0
        sigma = float(e.get("sigma") or 0)
        if mode == "blur" and not sigma:
            sigma = round(ff.auto_sigma(r) * strength, 1)
        elif mode == "blur" and kind != "pii" and sigma < min(r.w, r.h) * 0.25:
            msg = (f"{e.get('label', 'box')} at {r.css()} uses sigma {sigma:.0f} on a "
                   f"{r.w}x{r.h} mark - lettering stays readable; use sigma >= "
                   f"{ff.auto_sigma(r):.0f}, pixelate, or solid")
            if warn is not None and msg not in flagged:
                flagged.add(msg)
                warn.append(msg)
        key = (r.x, r.y, r.w, r.h, mode, sigma)
        g = grouped.setdefault(key, {"rect": r.as_dict(), "mode": mode, "sigma": sigma,
                                     "labels": [], "windows": []})
        g["labels"].append(e.get("label", ""))
        g["windows"] += wins
    out = []
    for g in grouped.values():
        merged = []
        for a, b in sorted(g["windows"]):
            if merged and a <= merged[-1][1] + 0.02:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in merged)
        out.append({"rect": g["rect"], "mode": g["mode"], "sigma": g["sigma"],
                    "mosaic": ff.mosaic_block(ff.Rect(**g["rect"])),
                    "enable": enable, "windows": merged,
                    "label": " / ".join(sorted({l for l in g["labels"] if l}))})
    return out


def step_windows(times: list[float], t0: float, t1: float, total: float,
                 minimum: float = 0.6) -> list[tuple[float, float]]:
    """One window per step caption, in walk order.

    screenrecord encodes on surface invalidation rather than on a clock, so a paused
    walkthrough collapses: several steps land on the same video instant and every caption
    would burn in on top of the others. When the recorded instants are not distinct the
    steps are spread evenly across the chapter instead.
    """
    n = len(times)
    if n == 0:
        return []
    end = min(t1, total) if total else t1
    if len({round(t, 3) for t in times}) == 1:
        step = max(minimum, (end - t0) / n)
        starts = [t0 + i * step for i in range(n)]
    else:
        starts = [min(max(t, t0), end) for t in times]
    for i in range(1, n):
        starts[i] = max(starts[i], starts[i - 1] + minimum)
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < n else max(end, s + minimum)
        out.append((s, max(e, s + minimum)))
    return out


def caption_plan(tl: dict, size: tuple[int, int], position: str = "bottom",
                 step_captions: bool = False, clock: bool = False) -> list[dict]:
    W, H = size
    caps: list[dict] = []
    total = tl["duration"]
    chapters = tl["chapters"]
    n = len(chapters)
    for i, c in enumerate(chapters):
        en = f"gte(t,{c['t0']:.3f})*lt(t,{c['t1']:.3f})"
        caps.append({"text": f"{i + 1} / {n}   {c['module']}", "enable": en,
                     "fontsize": max(26, int(H * 0.034)),
                     "y": int(H * (0.865 if position == "bottom" else 0.055))})
        if step_captions:
            inside = [e for e in tl["events"] if c["t0"] <= e["t"] < c["t1"] and e.get("label")]
            windows = step_windows([e["t"] for e in inside], c["t0"], c["t1"], total)
            for e, (t_start, t_end) in zip(inside, windows):
                caps.append({"text": e["label"], "y": int(H * (0.915 if position == "bottom" else 0.115)),
                             "fontsize": max(18, int(H * 0.021)),
                             "enable": f"gte(t,{t_start:.3f})*lt(t,{t_end:.3f})",
                             "boxcolor": "black@0.5"})
    if clock:
        caps.append({"expr": "%{pts\\:hms}", "x": int(W * 0.02),
                     "y": int(H * (0.955 if position == "bottom" else 0.16)),
                     "fontsize": max(16, int(H * 0.018)), "boxcolor": "black@0.45"})
    return caps


def segment_bar(tl: dict, size: tuple[int, int]) -> list[dict]:
    """drawbox cannot animate, so progress is one lit segment per chapter."""
    W, H = size
    h = max(6, int(H * 0.005))
    boxes = [{"rect": {"x": 0, "y": 0, "w": W, "h": h}, "color": "white@0.18"}]
    for c in tl["chapters"]:
        idx = c.get("index", 1) - 1
        n = max(1, len(tl["chapters"]))
        seg = W / n
        boxes.append({"rect": {"x": int(idx * seg), "y": 0,
                               "w": max(2, int(seg) - 3), "h": h},
                      "color": "white@0.9",
                      "enable": f"gte(t,{c['t0']:.3f})"})
    return boxes


def render(session, tl: dict, blurs: list[dict], opts: dict) -> dict:
    src = Path(opts.get("src") or session.path("recording"))
    if not src.exists():
        raise FileNotFoundError(f"{src} missing - record a take first")
    size = ff.dims(src)
    position = opts.get("position", "bottom")
    caps = caption_plan(tl, size, position, opts.get("step_captions", False),
                        opts.get("clock", False))
    boxes = segment_bar(tl, size) if opts.get("progress", True) else []
    graph = ff.build_graph(size, blurs, caps, position, boxes,
                           textdir=Path(session.root) / "render" / "captions")
    res_filters = len(blurs) + len(boxes) + len(caps)
    out = Path(opts["out"]) if opts.get("out") else session.path("final")
    vtt = out.with_suffix(".vtt")
    write_status(session.root, stage="rendering", filters=res_filters,
                 chapters=len(tl["chapters"]), duration=tl["duration"])
    ff.render(src, out, graph, tl["chapters"], vtt,
              crf=opts.get("crf", 22), preset=opts.get("preset", "veryfast"),
              progress=session.render / "progress.txt")
    write_status(session.root, stage="verifying", chapters=len(tl["chapters"]))
    return {"video": str(out), "vtt": str(vtt), "size": list(size),
            "graph_len": len(graph), "filters": len(blurs) + len(boxes) + len(caps),
            "chapters": len(tl["chapters"])}


def proof(session, tl: dict, blurs: list[dict], rendered: Path,
          max_samples: int = 2) -> dict:
    """Cut every masked box out of the finished file and judge it against the source still.

    `leak` = detail survived the blur. `blank` = the box sat on flat colour, which is
    not a pass, only the absence of something to read.
    """
    rendered = Path(rendered)
    frames_dir = session.render / "proof_frames"
    crops_dir = session.render / "proof_crops"
    for d in (frames_dir, crops_dir):
        if d.exists():
            shutil.rmtree(d)
    items, rows, missing = [], [], []
    for c in tl["chapters"]:
        mid = (c["t0"] + c["t1"]) / 2
        active = [b for b in blurs if any(a <= mid <= z for a, z in b["windows"])]
        if not active:
            missing.append(c["module"])
        ref = _reference(session, tl, c)
        span = c["t1"] - c["t0"]
        for k, t in enumerate([c["t0"] + min(0.8, span * 0.35),
                               c["t0"] + span * 0.65][:max_samples]):
            f = ff.frame(rendered, t, frames_dir / f"c{c['index']:02d}_{k}.png")
            tag = f"c{c['index']:02d}_{k}"
            for j, box in enumerate(b["rect"] for b in active):
                after = crop_regions(f, [box], crops_dir / tag, prefix=f"j{j}out")
                if not after:
                    continue
                before = (crop_regions(ref, [box], crops_dir / tag, prefix=f"j{j}src")
                          if ref else [])
                state = (mask_state(before[0], after[0], active[j].get("mode", "blur"))
                         if before else "no source still")
                rows.append({"chapter": c["module"], "at": round(t, 2),
                             "label": active[j]["label"], "box": list(box.values()),
                             "state": state})
                if before:
                    items.append((f"SRC {c['module']} {ff.tc(t)} {active[j]['label'][:20]}",
                                  before[0]))
                items.append((f"OUT {c['module']} {ff.tc(t)} "
                              f"{active[j]['label'][:20]} [{state}]", after[0]))
    sheet = (contact_sheet(items, session.render / "debrand_proof.png", cols=2)
             if items else None)
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["state"]] = tally.get(r["state"], 0) + 1
    attention = [r for r in rows if r["state"] in ("untouched", "review")]
    return {"sheet": str(sheet) if sheet else None, "regions": len(rows), "tally": tally,
            "attention": attention, "chapters_without_blur": missing, "rows": rows[:400]}


def _reference(session, tl: dict, chapter: dict) -> Path | None:
    """A pre-blur still of a screen inside this chapter, for change detection."""
    video = ff.dims(session.path("recording")) if session.path("recording").exists() else None
    for e in tl["events"]:
        if not e.get("screen") or not (chapter["t0"] <= e["t"] <= chapter["t1"]):
            continue
        p = session.stills / f"{e['screen']}.png"
        if p.exists() and (video is None or ff.dims(p) == video):
            return p
    return None
