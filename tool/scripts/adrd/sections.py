"""One clip per chapter, cut from the masked render.

A reviewer who wants the login screen should not scrub a four-minute take. The clips
are cut from the finished masked file, not from the raw capture, so a section can never
show branding the full video hides.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import ff
from .session import slugify

MANIFEST = "sections.json"
SOURCE = "render/demo_debranded.mp4"
MIN_SECONDS = 0.3


def read(root: Path) -> dict:
    try:
        return json.loads((Path(root) / "render" / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sections": []}


def build(root: Path) -> dict:
    root = Path(root)
    video = root / SOURCE
    if not video.is_file():
        raise FileNotFoundError("render the masked video first; sections are cut from it")
    timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
    out = root / "render" / "sections"
    out.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out.iterdir() if p.is_file()}
    total = round(ff.duration(video), 3)
    items: list[dict] = []
    for i, chapter in enumerate(timeline.get("chapters") or [], 1):
        t0 = max(0.0, float(chapter.get("t0") or 0))
        t1 = min(total, float(chapter.get("t1") or 0))
        seconds = round(t1 - t0, 3)
        if seconds < MIN_SECONDS:
            continue
        label = str(chapter.get("module") or chapter.get("title") or f"section-{i}")
        name = f"{i:02d}-{slugify(label)}.mp4"
        dest = out / name
        ff.clip(video, dest, t0, seconds)
        poster = dest.with_suffix(".jpg")
        ff.frame(dest, min(1.0, seconds / 2), poster, height=360)
        items.append({
            "n": i, "module": chapter.get("module") or "",
            "title": str(chapter.get("title") or label),
            "t0": round(t0, 3), "t1": round(t1, 3),
            "duration": round(ff.duration(dest), 3),
            "file": str(dest.relative_to(root)).replace("\\", "/"),
            "poster": str(poster.relative_to(root)).replace("\\", "/"),
            "bytes": dest.stat().st_size,
        })
    keep = {Path(it["file"]).name for it in items} | \
           {Path(it["poster"]).name for it in items}
    for stale in before - keep:
        (out / stale).unlink(missing_ok=True)
    manifest = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE, "source_duration": total,
        "count": len(items),
        "total_duration": round(sum(it["duration"] for it in items), 3),
        "sections": items,
    }
    (root / "render" / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n",
                                            encoding="utf-8")
    return manifest
