"""Files a run is performed against: the APK under test and the brief it is tested from.

Everything lands inside <session>/inputs/ and nothing else. Names are sanitised, sizes
are capped before a single byte is read, and writes are temp+fsync+rename so a half
received upload can never be mistaken for a complete one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

MAX_APK = 200 * 1024 * 1024
MAX_BRIEF = 10 * 1024 * 1024
BLOCK = 64 * 1024

KINDS = {
    "apk": {"max": MAX_APK, "exts": {".apk"}, "store": "app.apk"},
    "brief": {"max": MAX_BRIEF, "exts": {".md", ".markdown", ".txt", ".xml", ".pdf"},
              "store": "brief"},
}

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
_CASE_LINE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


class UploadError(ValueError):
    """Rejected before anything is written: bad kind, bad name, bad size."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def spec(kind: str) -> dict:
    try:
        return KINDS[kind]
    except KeyError:
        raise UploadError(f"unknown upload kind {kind!r}") from None


def safe_name(kind: str, name: str) -> str:
    """Basename only, no traversal, and the extension must match what the kind accepts."""
    spec(kind)
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base or ".." in base or "/" in base:
        raise UploadError("filename must be a plain basename")
    if not _SAFE.match(base):
        raise UploadError("filename may only use letters, digits, space, dot, dash, underscore")
    suffix = Path(base).suffix.lower()
    if suffix not in spec(kind)["exts"]:
        raise UploadError(f"{kind} must be one of {sorted(spec(kind)['exts'])}")
    return base


def target(root: Path, kind: str, name: str) -> Path:
    base = safe_name(kind, name)
    stored = base if kind == "brief" else "app.apk"
    p = (root / "inputs" / stored).resolve()
    try:
        p.relative_to((root / "inputs").resolve())
    except ValueError:
        raise UploadError("resolved outside the session inputs dir") from None
    return p


def stream_to(root: Path, kind: str, name: str, rfile, length: int) -> dict:
    """Read exactly `length` bytes off the wire into the session. Oversize is refused
    before the body is read, so a hostile client cannot make us buffer it."""
    limit = spec(kind)["max"]
    if length <= 0:
        raise UploadError("empty body: send the file bytes as the request body")
    if length > limit:
        raise UploadError(f"{kind} larger than {limit} bytes (got {length})", 413)
    p = target(root, kind, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".part.{hashlib.sha1(name.encode()).hexdigest()[:8]}")
    digest = hashlib.sha256()
    got = 0
    try:
        with tmp.open("wb") as fh:
            while got < length:
                block = rfile.read(min(BLOCK, length - got))
                if not block:
                    break
                got += len(block)
                digest.update(block)
                fh.write(block)
            fh.flush()
            os.fsync(fh.fileno())
        if got != length:
            raise UploadError(f"truncated upload: got {got} of {length} bytes")
        tmp.replace(p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"kind": kind, "name": p.name, "bytes": got, "sha256": digest.hexdigest(),
            "path": str(p.resolve().relative_to(root.resolve()))}


def _brief_files(root: Path) -> list[Path]:
    """Anything in inputs/ that is not the APK and not a half-written temp."""
    d = root / "inputs"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.name != "app.apk" and ".part." not in p.name)


def listed(root: Path) -> dict:
    apk = root / "inputs" / "app.apk"
    briefs = _brief_files(root)
    rel = lambda p: str(p.resolve().relative_to(root.resolve()))
    return {
        "apk": None if not apk.is_file() else
               {"name": apk.name, "bytes": apk.stat().st_size, "path": rel(apk)},
        "brief": None if not briefs else
                 {"name": briefs[-1].name, "bytes": briefs[-1].stat().st_size,
                  "path": rel(briefs[-1]), "count": len(briefs)},
    }


def _brief_text(p: Path) -> str:
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise UploadError(
                "PDF briefs need pypdf in the pinned venv: "
                ".venv/bin/python -m pip install pypdf  - or upload a .md/.txt brief") from None
        reader = PdfReader(str(p))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return p.read_text(encoding="utf-8", errors="replace")


def build_cases(root: Path) -> dict:
    """Cases come from the brief when there is one. With no brief the run falls back to
    the screen inventory `flow` derived, so every known route still gets a check."""
    briefs = _brief_files(root)
    origin, cases = "brief", []
    if briefs:
        text = _brief_text(briefs[-1])
        section = ""
        for line in text.splitlines():
            h = _HEADING.match(line)
            if h:
                section = h.group(1).strip()
                continue
            m = _CASE_LINE.match(line)
            if m:
                title = m.group(1).strip()
                if title:
                    cases.append({"title": title, "section": section,
                                  "source": briefs[-1].name})
    else:
        origin = "flow"
        flow = read_json(root / "flow_map.json", {}) or {}
        for route in flow.get("routes", []):
            name = route if isinstance(route, str) else (route.get("name") or route.get("route") or "")
            if name:
                cases.append({"title": f"Reach {name} and check it renders",
                              "section": str(route.get("module", "")) if isinstance(route, dict) else "",
                              "source": "flow_map.json"})
    numbered = [{"id": f"C{i + 1:02d}", **c} for i, c in enumerate(cases)]
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "cases.json").write_text(json.dumps({"origin": origin, "cases": numbered},
                                                indent=2) + "\n", encoding="utf-8")
    lines = [f"# Test cases ({origin})", ""]
    for c in numbered:
        where = f" _{c['section']}_" if c.get("section") else ""
        lines.append(f"- [ ] **{c['id']}** {c['title']}{where}")
    (root / "CASES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"origin": origin, "count": len(numbered),
            "cases": "cases.json", "markdown": "CASES.md"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
