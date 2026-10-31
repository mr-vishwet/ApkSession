"""Final bundle: both videos, every evidence image, the brief it was run against.

Everything lands in one folder and one zip, with a manifest that hashes each file, so
the bundle can be re-verified later without trusting the copy you were sent.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

TOOL = "app-demo-recorder v1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _copy(src: Path, dest: Path, label: str) -> dict | None:
    if not src.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = dest.parent / f"{label}__{src.name}" if label else dest
    if out.resolve() != src.resolve():
        shutil.copy2(src, out)
    return {"path": str(out), "bytes": out.stat().st_size}


def _evidence_groups(session) -> list[tuple[str, Path, str]]:
    """(folder, source, label prefix) - grouped so a reviewer can follow the order."""
    return [
        ("evidences/screens", session.stills, "screen"),
        ("evidences/box-placement", session.annotated, "placement"),
        ("evidences/video-frames", session.render / "proof_frames", "frame"),
        ("evidences/masked-regions", session.render / "proof_crops", "region"),
    ]


def readme(session, tl: dict, st: dict, brief: str, include_raw: bool) -> str:
    rend = session.read("render", {})
    proof = rend.get("proof", {})
    L = [
        "=" * 76, f"  {st.get('app', 'app')} - recorded walkthrough bundle",
        f"  built {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {TOOL}",
        "=" * 76, "",
        "WHAT THIS IS",
        "  A scripted walkthrough of the app, recorded on an Android device, with the",
        "  client's brand marks masked so the UI can be shown outside that engagement.",
        "",
        "  The app source was not modified. Routes and modules were read statically;",
        "  the screens shown are the real shipped app.",
        "",
        "FILES",
        "  video/demo_debranded.mp4   the shareable recording, chapters embedded",
        "  video/chapters.vtt         chapter list with timecodes for any player",
        "  SESSION_DOC.md             what happens at every timestamp",
        "  FLOW_MAP.md                routes and modules read from the source",
        "  BRIEF.txt                  the instructions this run was performed against",
        "  evidences/                 every image the run produced, grouped by purpose",
    ]
    if include_raw:
        L.append("  video/recording_UNMASKED.mp4   UNMASKED - internal use only, not for sharing")
    L += [
        "  manifest.json              sha256 of every file in this bundle",
        "",
        "RUN SUMMARY",
        f"  device        {st.get('device_model')} ({st.get('serial')}) "
        f"{st.get('screen_size', ['', ''])[0]}x{st.get('screen_size', ['', ''])[1]}",
        f"  package       {st.get('package')}",
        f"  video length  {tl.get('duration', 0):.1f}s "
        f"({tl.get('compression', 1.0) * 100:.0f}% of wall time; the recorder drops idle frames)",
        f"  chapters      {len(tl.get('chapters', []))}",
        f"  timing        {(tl.get('align') or {}).get('method')}, "
        f"{(tl.get('align') or {}).get('snapped', 0)} transitions matched to scene cuts, "
        f"residual {(tl.get('align') or {}).get('residual', -1):.2f}s",
        f"  mask check    {proof.get('tally', {}) or 'not run'}",
        "",
        "BEFORE YOU SHOW THIS ANYWHERE",
        "  1. Read evidences/sheets/*sweep*.png cover to cover. The automated check only",
        "     judges boxes someone declared; branding outside them is invisible to it.",
        "  2. Confirm no real names, emails, avatars or partner data are legible. Those",
        "     are personal data, not branding, and need boxes of their own.",
        "  3. Never send video/recording_UNMASKED.mp4 outside the engagement.",
        "",
        "HOW TO REPRODUCE",
        "  adrd launch --session DIR --clear",
        "  adrd take --session DIR          # records; adrd scout does not",
        "  adrd brand ... && adrd render --session DIR --step-captions",
        "  adrd proof --session DIR && adrd sweep --session DIR && adrd doc --session DIR",
        "",
        "-" * 76, "BRIEF THIS RUN WAS PERFORMED AGAINST", "",
        (brief.strip() or "(none supplied - pass --brief or --brief-file)"), "",
    ]
    return "\n".join(L)


def build(session, brief_text: str = "", brief_file: str | None = None,
          include_raw: bool = True, dest: str | None = None) -> Path:
    st, tl = session.state(), session.read("timeline")
    out = Path(dest) if dest else session.root / "export"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    brief = brief_text
    if brief_file:
        brief = Path(brief_file).read_text()
    if not brief:
        brief = session.state().get("brief", "")

    files: list[dict] = []

    def keep(src: Path, name: str) -> None:
        rec = _copy(src, out / name, "")
        if rec:
            files.append({**rec, "name": name})

    keep(session.path("final"), "video/demo_debranded.mp4")
    keep(session.path("vtt"), "video/chapters.vtt")
    keep(session.path("doc"), "SESSION_DOC.md")
    keep(session.path("flow").with_name("FLOW_MAP.md"), "FLOW_MAP.md")
    if include_raw:
        keep(session.path("recording"), "video/recording_UNMASKED.mp4")

    for folder, src_dir, label in _evidence_groups(session):
        if not src_dir.exists():
            continue
        for i, f in enumerate(sorted(src_dir.rglob("*"))):
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg") \
                    and f.stat().st_size < 8_000_000:
                rel = Path(folder) / f.relative_to(src_dir)
                rec = _copy(f, out / rel, f"{i:03d}")
                if rec:
                    files.append({**rec, "name": str(rel)})
    for sheet in ("screens_contact_sheet", "debrand_proof", "sweep"):
        p = session.render / f"{sheet}.png"
        if p.exists():
            rec = _copy(p, out / "evidences" / "sheets" / p.name, "")
            if rec:
                files.append({**rec, "name": f"evidences/sheets/{p.name}"})

    (out / "BRIEF.txt").write_text(brief.strip() or "(none supplied)\n")
    (out / "README.txt").write_text(readme(session, tl, st, brief, include_raw))

    manifest = {
        "tool": TOOL, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app": st.get("app"), "package": st.get("package"),
        "device": st.get("device_model"), "serial": st.get("serial"),
        "screen": st.get("screen_size"), "duration": tl.get("duration"),
        "chapters": tl.get("chapters", []), "align": tl.get("align", {}),
        "compression": tl.get("compression"), "masks": session.read("branding", {})
        .get("entries", []), "leak_check": session.read("render", {}).get("proof", {})
        .get("tally", {}), "unmasked_included": include_raw,
        "files": sorted(({**f, "sha256": sha256(Path(f["path"]))} for f in files),
                        key=lambda f: f["name"]),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if include_raw:
        (out / "video" / "READ_ME_FIRST.txt").write_text(
            "recording_UNMASKED.mp4 still shows the client's brand and any real user "
            "data on screen. Keep it inside the engagement. The file to share is "
            "demo_debranded.mp4.\n")

    archive = out.parent / f"{out.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out))
    return archive
