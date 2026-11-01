"""Session directory layout and the JSON records every stage reads or writes."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SCHEMA = 1
FILES = {
    "state": "state.json",
    "flow": "flow_map.json",
    "walk": "walk.json",
    "events": "events.json",
    "timeline": "timeline.json",
    "branding": "branding.json",
    "recording": "recording.mp4",
    "render": "render.json",
    "final": "render/demo_debranded.mp4",
    "vtt": "render/demo_chapters.vtt",
    "doc": "render/SESSION_DOC.md",
    "report": "render/SESSION_DOC.pdf",
    "sheet": "render/screens_contact_sheet.png",
    "proof": "render/debrand_proof.png",
    "bundle": "export.zip",
}


def slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:48] or "app"


class Session:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.stills = self.root / "stills"
        self.annotated = self.root / "stills_annotated"
        self.raw = self.root / "raw"
        self.render = self.root / "render"
        for d in (self.stills, self.annotated, self.raw, self.render):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(cls, base: Path | str, app_name: str) -> "Session":
        base = Path(base)
        stamp = time.strftime("%Y%m%d-%H%M")
        root = base / f"{slugify(app_name)}-{stamp}"
        n = 1
        while root.exists():
            n += 1
            root = base / f"{slugify(app_name)}-{stamp}-{n}"
        s = cls(root)
        s.write("state", {"schema": SCHEMA, "app": app_name,
                          "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "rec_start": None, "segments": []})
        return s

    def path(self, key: str) -> Path:
        return self.root / FILES[key]

    def state(self) -> dict:
        return self.read("state")

    def update_state(self, **kw: Any) -> dict:
        st = self.state()
        st.update(kw)
        self.write("state", st, quiet=True)
        return st

    def read(self, key: str, default: Any = None) -> Any:
        p = self.path(key)
        if not p.exists():
            if default is not None:
                return default
            raise FileNotFoundError(f"{p} missing - run the earlier stage first")
        return json.loads(p.read_text())

    def write(self, key: str, data: Any, quiet: bool = False) -> Path:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        if not quiet:
            print(f"wrote {p}")
        return p

    def exists(self, key: str) -> bool:
        return self.path(key).exists()


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def save_json(path: Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path
