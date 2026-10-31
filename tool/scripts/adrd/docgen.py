"""The timestamped write-up that ships alongside the video."""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import ff

ACTION_WORDS = {
    "tap": "tap", "long_press": "press and hold", "swipe": "swipe", "key": "key",
    "text": "type", "wait": "pause", "screenshot": "capture", "back": "system back",
    "hide_keyboard": "dismiss keyboard",
}


def _pt(op: dict, x: str, y: str) -> str:
    if f"r{x}" in op and f"r{y}" in op:
        return f"{op[f'r{x}']:.2f},{op[f'r{y}']:.2f}"
    return f"{op.get(x, '?')},{op.get(y, '?')}"


def _ops(walk_step_do: list[dict]) -> str:
    bits = []
    for op in walk_step_do:
        k = op["op"]
        if k == "wait":
            bits.append(f"wait {op.get('s', 1)}s")
        elif k in ("tap", "long_press"):
            bits.append(f"{ACTION_WORDS[k]} @ {_pt(op, 'x', 'y')}")
        elif k == "swipe":
            bits.append(f"swipe {_pt(op, 'x', 'y')} -> {_pt(op, 'x2', 'y2')}")
        elif k == "text":
            bits.append("type " + ("<redacted>" if op.get("secret")
                                   else f"'{op.get('value', '')}'"))
        elif k == "key":
            bits.append(f"key {op.get('code', 4)}")
        else:
            bits.append(ACTION_WORDS.get(k, k))
    return ", ".join(bits)


def _routes_for(flow: dict | None, module: str) -> list[str]:
    if not flow:
        return []
    for m in flow.get("modules", []):
        if m["module"].lower() == module.lower():
            return [r for r in m["routes"] if r]
    return []


def _screens_for(flow: dict | None, module: str) -> list[str]:
    if not flow:
        return []
    for m in flow.get("modules", []):
        if m["module"].lower() == module.lower():
            return m.get("screens", [])[:14]
    return []


def _files_for(flow: dict | None, module: str) -> list[str]:
    if not flow:
        return []
    seen = []
    for r in flow.get("routes", []):
        if r["module"].lower() == module.lower():
            tag = f"{r['file']}:{r['line']}"
            if tag not in seen:
                seen.append(tag)
    return seen[:10]


def chapter_map(walk: dict) -> dict[int, dict]:
    return {i: s for i, s in enumerate(walk.get("steps", []))}


def _redactor(tokens: list[str]):
    """The shareable doc must not name the client, so every brand token is folded
    into a neutral phrase - including ones I typed myself as box labels."""
    pat = re.compile("|".join(re.escape(x) for x in sorted({t2 for t2 in tokens if t2 and len(t2) > 2},
                                                           key=len, reverse=True)), re.I)
    return lambda s: pat.sub("the app", s)


def markdown(session, tl: dict, walk: dict, branding: dict, flow: dict | None,
             proof: dict | None, align: dict | None, device: dict,
             redact: list[str] | None = None) -> str:
    st = session.state()
    steps = chapter_map(walk)
    chapters = tl["chapters"]
    total = tl["duration"]
    L: list[str] = []
    red = _redactor(redact or [])
    A = lambda s: L.append(red(s))

    A(f"# Walkthrough - {st.get('app', 'app')}")
    A("")
    A(f"- Recorded: {st.get('created')}")
    A(f"- Device: {device.get('model', 'unknown')} ({device.get('serial', '?')}) "
      f"at {device.get('size', ['?', '?'])[0]}x{device.get('size', ['?', '?'])[1]}")
    A(f"- Build: `{st.get('package') or 'unknown id'}` on "
      f"{st.get('install_kind', 'pre-installed')}")
    final = Path(session.read("render", {}).get("video") or session.path("final"))
    A(f"- Shareable video: `{final.name}` - {ff.hms(total)}, {len(chapters)} chapters, "
      f"brand marks masked")
    A(f"- Raw capture: `{Path(tl.get('video') or session.path('recording')).name}` - "
      f"unmasked, never send this one")
    fit = tl.get("align") or align or {}
    if fit.get("method") == "cut anchored":
        A(f"- Timing: {fit['method']}, {fit.get('snapped', 0)} of "
          f"{fit.get('anchors', 0)} transitions matched to the video's own scene cuts, "
          f"mean residual {fit.get('residual', -1):.2f}s")
        if 0 < fit.get("scale", 1.0) < 0.85:
            A(f"  - the file runs at {fit['scale'] * 100:.0f}% of wall time: "
              "screenrecord drops idle frames, so pauses do not appear in it. Every "
              "timecode below is **video time**, which is what a viewer sees.")
    else:
        A(f"- Timing: host clock shifted {tl['offset']:.2f}s, not verified against "
          "scene cuts - scrub each chapter start before sharing")
    A("")
    A("> Screens and flows are the real app. Brand marks, app name and logos are "
     "masked for external review.")
    A("")

    A("## Contents")
    A("")
    A("| # | Module | Starts | Ends | Length | What it shows |")
    A("|---|---|---|---|---|---|")
    for c in chapters:
        A(f"| {c['index']} | {c['module']} | `{ff.tc(c['t0'])}` | `{ff.tc(c['t1'])}` | "
          f"{c['t1'] - c['t0']:.0f}s | {c['opened_with'] or '-'} |")
    A("")

    A("## Step by step")
    A("")
    for c in chapters:
        A(f"### {c['index']}. {c['module']}  `{ff.tc(c['t0'])} - {ff.tc(c['t1'])}`")
        A("")
        routes = _routes_for(flow, c["module"])
        if routes:
            A(f"Routes proven: {', '.join(f'`{r}`' for r in routes[:8])}")
        screens = _screens_for(flow, c["module"])
        if screens:
            A(f"Screens in module: {', '.join(screens)}")
        files = _files_for(flow, c["module"])
        if files:
            A(f"Source: {', '.join(f'`{f}`' for f in files[:6])}")
        if routes or screens or files:
            A("")
        A("| time | action | detail |")
        A("|---|---|---|")
        last = c["index"] == len(chapters)
        for e in tl["events"]:
            if not (c["t0"] <= e["t"] < c["t1"] or (last and e["t"] >= c["t0"])):
                continue
            step = steps.get(e["i"], {})
            detail = e.get("label") or step.get("label") or ""
            ops = _ops(step.get("do", []))
            marks = []
            if e.get("screen"):
                marks.append(f"screen `{e['screen']}`")
            if e.get("mismatch"):
                marks.append("screen did not match calibration")
            if e.get("typed"):
                marks.append(f"input {e['typed']}")
            A(f"| `{ff.tc(e['t'])}` | {detail} | {ops}"
              + (f" - {'; '.join(marks)}" if marks else "") + " |")
        A("")

    A("## De-branding log")
    A("")
    entries = branding.get("entries", [])
    if not entries:
        A("Nothing masked. This video still shows the client brand.")
    else:
        A("| region | where | how | why |")
        A("|---|---|---|---|")
        for e in entries:
            A(f"| {e.get('label', '')} | {e.get('scope', '')} | "
              f"`{e.get('mode', 'blur')}` | {e.get('note', '') or '-'} |")
        A("")
        if proof:
            tally = ", ".join(f"{v} {k}" for k, v in proof.get("tally", {}).items())
            A(f"Checked {proof.get('regions', 0)} masked region samples cut out of the "
              f"finished file: {tally or 'no samples'}.")
            bad = [r for r in proof.get("attention", []) if r["state"] == "untouched"]
            if bad:
                A("")
                A(f"**{len(bad)} sample(s) show a box that changed nothing** - wrong "
                  f"place or wrong time window: "
                  f"{', '.join(sorted({str(l['label']) for l in bad}))}")
            if proof.get("chapters_without_blur"):
                A(f"Chapters with no mask landing in them: "
                  f"{', '.join(proof['chapters_without_blur'])}")
            if proof.get("sheet"):
                A(f"Region proof sheet: `{Path(proof['sheet']).name}`")
    A("")

    A("## How this was made")
    A("")
    A("1. Routes and modules read statically from the app source - the source was not modified.")
    A("2. Screen coordinates calibrated once, then replayed as a scripted walk.")
    A("3. Device screen recorded while the walk ran; each step timestamped.")
    A("4. Tap times correlated with the recording's own scene cuts to align clocks.")
    A("5. Brand marks masked and chapter labels burned in during a single ffmpeg pass.")
    A("")
    A("## Known gaps")
    A("")
    A("- Module names come from folder structure; a route in an unexpected folder may "
      "be filed under the wrong chapter.")
    A("- Blurs follow the calibrated box geometry. If the UI shifts on a different "
      "device size, re-check the proof sheet.")
    A("- The region check only judges boxes that were declared. Branding sitting "
      "outside those boxes is invisible to it - read `sweep.png` before sharing.")
    A("- No audio track. Nothing in this file narrates itself.")
    return "\n".join(L) + "\n"


def artifacts(session, extra: dict | None = None) -> Path:
    st = session.state()
    lines = [f"# Session artifacts - {st.get('app', 'app')}", "",
             f"Created {st.get('created')}", "", "| file | what |", "|---|---|"]
    order = [
        ("state", "device, package, timing base"),
        ("flow", "routes/modules read from source"),
        ("walk", "the scripted walk - replayable"),
        ("events", "raw step timestamps"),
        ("timeline", "chapters + per-screen windows"),
        ("branding", "masked regions"),
        ("recording", "uncorrected screen capture"),
        ("final", "the video to share"),
        ("vtt", "chapter captions"),
        ("doc", "timestamped write-up"),
        ("sheet", "every screen, for box placement"),
        ("proof", "masked regions cut out of the final video"),
    ]
    for key, what in order:
        p = session.path(key)
        if p.exists():
            lines.append(f"| `{p.relative_to(session.root)}` | {what} |")
    for k, v in (extra or {}).items():
        lines.append(f"| `{v}` | {k} |")
    out = session.root / "ARTIFACTS.md"
    out.write_text("\n".join(lines) + "\n")
    return out
