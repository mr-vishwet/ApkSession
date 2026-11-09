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
import time
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


RUN_FILES = ("app.apk", "run.log", "run.json")
# the run writes these into inputs/ itself; a brief must never be confused with them

SCOPES = {
    "focused": {
        "label": "Focused",
        "brief": "required",
        "detail": "Only the steps written in the spec are tested. A spec is required.",
    },
    "full": {
        "label": "Full app",
        "brief": "optional",
        "detail": "The spec steps run first, then every route the app exposes.",
    },
}
DEFAULT_SCOPE = "focused"


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
    """Anything in inputs/ that is not the APK, not one of the run's own records, and not
    a half-written temp. The run writes its log and stage record next to the brief, and
    run.log sorts after every real filename - counting it as the brief made the page
    report the log as the instructions and offered to build cases out of it."""
    d = root / "inputs"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.name not in RUN_FILES and ".part." not in p.name)


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


def _brief_cases(p: Path) -> tuple[list[dict], str]:
    """A spec is read as headings plus list items. A prose or PDF spec with no list still
    narrows the scope: each heading becomes one flow to check. A spec with neither is
    refused instead of being turned into a confident empty run."""
    text = _brief_text(p)
    section, listed, headings = "", [], []
    for line in text.splitlines():
        h = _HEADING.match(line)
        if h:
            section = h.group(1).strip()
            headings.append(section)
            continue
        m = _CASE_LINE.match(line)
        if m and m.group(1).strip():
            listed.append({"title": m.group(1).strip(), "section": section or "Spec",
                           "from": "spec", "source": p.name})
    if listed:
        return listed, text
    flows = headings[1:] if len(headings) > 1 else headings
    titled = [{"title": h, "section": "Spec", "from": "spec", "source": p.name}
              for h in flows]
    if not titled:
        raise UploadError(
            "the spec has no test steps: add one list item per check, or one heading "
            "per flow", 400)
    return titled, text


def _flow_cases(root: Path, mentioned: str = "") -> list[dict]:
    """Every route the static scan found, minus the ones the spec already mentions.
    That is what makes Full scope additive rather than a duplicate of the spec."""
    flow = read_json(root / "flow_map.json", {}) or {}
    blob = mentioned.casefold()
    seen, out = set(), []
    for route in flow.get("routes", []):
        if isinstance(route, str):
            name, module = route, ""
        else:
            name = route.get("name") or route.get("route") or ""
            module = str(route.get("module", ""))
        if not name or name in seen:
            continue
        if blob and (name.casefold() in blob or
                     (len(module) > 2 and module.casefold() in blob)):
            continue
        seen.add(name)
        out.append({"title": f"Reach {name} and check it renders",
                    "section": module or "Whole app", "from": "flow",
                    "source": "flow_map.json"})
    return out


def build_cases(root: Path, scope: str | None = None) -> dict:
    """Focused tests the spec and nothing else, so it demands one. Full tests the spec
    first and then the rest of the app inventory, so the spec is optional there."""
    scope = scope or read_scope(root)
    if scope not in SCOPES:
        raise UploadError(f"scope must be one of {sorted(SCOPES)}")
    briefs = _brief_files(root)
    brief_cases: list[dict] = []
    spec_text = ""
    if briefs:
        brief_cases, spec_text = _brief_cases(briefs[-1])
    elif scope == "focused":
        raise UploadError(
            "Focused scope needs a spec. Upload a .md, .pdf or .txt with the steps to "
            "check, or switch to Full app.", 400)
    flow_cases = _flow_cases(root, spec_text if scope == "full" else "")
    cases = brief_cases + (flow_cases if scope == "full" else [])
    warning = None
    if not cases:
        # Full scope on an APK-only session has nothing statically scanned yet; the walk
        # the session already holds is still a real run, so this is stated, not blocked.
        warning = ("no flow inventory in this session: this run follows the walk it "
                   "already has, with no case list")
    origin = ("mixed" if brief_cases and flow_cases
              else "brief" if brief_cases else "flow")
    numbered = [{"id": f"C{i + 1:02d}", **c} for i, c in enumerate(cases)]
    counts = {"from_spec": len(brief_cases),
              "from_flow": len(flow_cases) if scope == "full" else 0}
    sections = list(dict.fromkeys(c.get("section") or "Spec" for c in numbered))
    record = {"scope": scope, "origin": origin, "count": len(numbered),
              "counts": counts, "spec": briefs[-1].name if briefs else None,
              "sections": sections, "cases": numbered}
    if warning:
        record["warning"] = warning
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "cases.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Test cases ({scope})", "",
             f"Scope: {SCOPES[scope]['label']} - {SCOPES[scope]['detail']}",
             f"Cases: {counts['from_spec']} from the spec, "
             f"{counts['from_flow']} beyond it.", ""]
    if warning:
        lines.append(f"_{warning}_")
    current = None
    for c in numbered:
        if c.get("section") != current:
            current = c.get("section")
            lines += [f"## {current}", ""]
        extra = " _(beyond the spec)_" if c.get("from") == "flow" else ""
        lines.append(f"- [ ] **{c['id']}** {c['title']}{extra}")
    (root / "CASES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"origin": origin, "scope": scope, "count": len(numbered),
            "counts": counts, "warning": warning,
            "cases": "cases.json", "markdown": "CASES.md"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_scope(root: Path) -> str:
    name = (read_json(root / "scope.json", {}) or {}).get("scope")
    return name if name in SCOPES else DEFAULT_SCOPE


def write_scope(root: Path, name: str) -> str:
    if name not in SCOPES:
        raise UploadError(f"scope must be one of {sorted(SCOPES)}")
    (root / "scope.json").write_text(json.dumps({"scope": name,
                                                 "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                                                indent=2) + "\n", encoding="utf-8")
    return name
