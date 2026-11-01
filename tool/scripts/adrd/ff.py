"""ffmpeg/ffprobe wrappers plus the de-brand and caption filtergraph builder."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .env import find_ffmpeg, find_ffprobe, find_font


class FFError(RuntimeError):
    pass


def _exe(which: str) -> Path:
    p = find_ffmpeg() if which == "ffmpeg" else find_ffprobe()
    if not p:
        raise FFError("ffmpeg not available; run: python3 -m pip install imageio-ffmpeg")
    return p


def run(args: list, timeout: float = 3600) -> subprocess.CompletedProcess:
    cp = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                        timeout=timeout)
    if cp.returncode != 0:
        tail = "\n".join(cp.stderr.strip().splitlines()[-20:])
        raise FFError(f"ffmpeg failed (exit {cp.returncode}):\n{tail}")
    return cp


def _dur(text: str) -> float:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        return 0.0
    h, mnt, s = (float(x) for x in m.groups())
    return h * 3600 + mnt * 60 + s


def probe(path: Path | str) -> dict:
    path = Path(path)
    """imageio-ffmpeg ships ffmpeg but no ffprobe, so read the media header off stderr."""
    if find_ffprobe():
        out = run([_exe("ffprobe"), "-v", "error", "-show_entries",
                   "format=duration:stream=width,height,r_frame_rate",
                   "-of", "default=nw=1", path], timeout=120).stdout
        d: dict = {}
        for line in out.splitlines():
            k, _, v = line.partition("=")
            if v.strip():
                d.setdefault(k.strip(), v.strip())
        return d
    cp = subprocess.run([str(_exe("ffmpeg")), "-hide_banner", "-i", str(path)],
                        capture_output=True, text=True)
    blob = cp.stderr
    d = {"duration": f"{_dur(blob):.3f}"}
    video = next((ln for ln in blob.splitlines() if ": Video:" in ln), "")
    wh = re.search(r"(\d{2,5})x(\d{2,5})", video)
    if wh:
        d["width"], d["height"] = wh.group(1), wh.group(2)
    fps = re.search(r"([\d.]+)\s*fps", video)
    if fps:
        d["r_frame_rate"] = fps.group(1)
    return d


def duration(path: Path) -> float:
    return float(probe(path).get("duration", 0.0) or 0.0)


def fps(path: Path) -> float:
    return float(probe(path).get("r_frame_rate", 0) or 0.0)


def dims(path: Path) -> tuple[int, int]:
    p = probe(path)
    return int(float(p.get("width", 0))), int(float(p.get("height", 0)))


def hms(seconds: float) -> str:
    s = max(0.0, float(seconds))
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def tc(seconds: float) -> str:
    """Short timecode for docs and captions."""
    s = max(0.0, float(seconds))
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


def scene_changes(video: Path, threshold: float = 0.4, timeout: float = 1800) -> list[float]:
    """Screen-transition times straight off the video; used to align our clock.

    scdet logs `lavfi.scd.score` / `lavfi.scd.time`; a flat-panel cut scores ~1,
    a busy Flutter transition far more, so the default stays low and the fitter
    tolerates the extra hits.
    """
    cp = run([_exe("ffmpeg"), "-hide_banner", "-i", video, "-vf",
              f"scdet=threshold={threshold}", "-an", "-f", "null", "-"],
             timeout=timeout)
    found = set()
    for line in cp.stderr.splitlines():
        m = re.search(r"lavfi\.scd\.score:\s*([\d.]+),\s*lavfi\.scd\.time:\s*([\d.]+)", line)
        if m:
            found.add(round(float(m.group(2)), 3))
    return sorted(found)


def concat(parts: list[Path], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in parts))
    args = [_exe("ffmpeg"), "-hide_banner", "-f", "concat", "-safe", "0", "-i", listing]
    try:
        run(args + ["-c", "copy", "-y", out], timeout=1200)
        got, want = duration(out), sum(duration(p) for p in parts)
        if got < want * 0.95:
            raise FFError(f"stream-copy lost frames ({got:.1f}s of {want:.1f}s)")
    except FFError:
        run(args + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-y", out], timeout=3600)
    finally:
        listing.unlink(missing_ok=True)
    return out


def frame(video: Path, at: float, out: Path | str, height: int | None = None) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [_exe("ffmpeg"), "-hide_banner", "-ss", f"{max(0.0, at):.3f}", "-i", video,
            "-frames:v", "1", "-y"]
    if height:
        args += ["-vf", f"scale=-2:{height}"]
    args.append(out)
    run(args, timeout=300)
    return out


def esc(text: str) -> str:
    """drawtext escaping for a value wrapped in single quotes: backslash, colon, quote
    and % are graph-significant.

    This cannot carry an apostrophe. ffmpeg's parser has no escape for a quote inside
    single quotes, so `\\'` simply closes the option and the commas of the following
    enable='gte(t,..)*lt(t,..)' stop being protected. Real caption text therefore goes
    through textfile= (see _drawtext), and only fixed strings use this.
    """
    return (str(text).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                .replace("%", "\\%").replace("\n", " ").replace("[", "\\[").replace("]", "\\]"))


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def parse(cls, value: str) -> "Rect":
        parts = [int(float(p)) for p in re.split(r"[,\s]+", value.strip()) if p.strip()]
        if len(parts) != 4:
            raise ValueError(f"rect must be x,y,w,h got {value!r}")
        return cls(*parts)

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def clamped(self, width: int, height: int) -> "Rect":
        x = max(0, min(self.x, width - 4))
        y = max(0, min(self.y, height - 4))
        return Rect(x, y, max(4, min(self.w, width - x)), max(4, min(self.h, height - y)))

    def css(self) -> str:
        return f"{self.x},{self.y} {self.w}x{self.h}"


def auto_sigma(r: Rect) -> float:
    """Blur must scale with the mark: sigma 22 leaves a 140px wordmark fully readable."""
    return max(24.0, round(min(r.w, r.h) * 0.38, 1))


def mosaic_block(r: Rect) -> int:
    return max(8, int(min(r.w, r.h) * 0.2))


def _region_chain(label: str, nxt: str, r: Rect, mode: str, sigma: float,
                  enable: str) -> list[str]:
    """Blur/pixelate a rectangle by routing only that crop through the filter."""
    if mode == "solid":
        return [f"[{label}]drawbox=x={r.x}:y={r.y}:w={r.w}:h={r.h}"
                f":color=black@1:t=fill:enable='{enable}'[{nxt}]"]
    a, b, g = f"{nxt}_a", f"{nxt}_b", f"{nxt}_g"
    if mode == "pixelate":
        k = mosaic_block(r)
        steps = [f"crop={r.w}:{r.h}:{r.x}:{r.y}", f"scale=iw/{k}:ih/{k}:flags=area",
                 f"scale={r.w}:{r.h}:flags=neighbor"]
    else:
        s = auto_sigma(r) if not sigma else sigma
        steps = [f"crop={r.w}:{r.h}:{r.x}:{r.y}", f"gblur=sigma={s}"]
    return [
        f"[{label}]split=2[{a}][{b}]",
        f"[{b}]{','.join(steps)}[{g}]",
        f"[{a}][{g}]overlay={r.x}:{r.y}:enable='{enable}'[{nxt}]",
    ]


def _drawtext(cap: dict, W: int, H: int, position: str) -> str:
    fs = int(cap.get("fontsize") or max(18, H * 0.026))
    x = int(cap.get("x", W * 0.025))
    y = cap.get("y")
    if y is None:
        y = int(H * 0.885) if position == "bottom" else int(H * 0.075)
    if "expr" in cap:
        text = f"text='{cap['expr']}'"
    elif cap.get("textfile"):
        text = f"textfile={cap['textfile']}"
    else:
        text = f"text='{esc(cap.get('text', ''))}'"
    body = (
        f"drawtext=fontfile={find_font()}:{text}:fontsize={fs}"
        f":fontcolor={cap.get('fontcolor', 'white')}"
        f":box=1:boxcolor={cap.get('boxcolor', 'black@0.62')}"
        f":boxborderw={max(6, int(fs * 0.4))}:x={x}:y={int(y)}"
    )
    if cap.get("enable"):
        body += f":enable='{cap['enable']}'"
    return body


def build_graph(video_size: tuple[int, int], blurs: list[dict],
                captions: list[dict], position: str = "bottom",
                boxes: list[dict] | None = None,
                textdir: Path | None = None) -> str:
    """One graph: brand rects blurred in-window, fill boxes, then caption overlays.

    With a textdir, every caption is written to its own file and handed to drawtext as
    textfile=, because filtergraph quoting cannot carry an apostrophe.
    """
    W, H = video_size
    if textdir is not None:
        textdir.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(captions):
            if c.get("text") is not None and "expr" not in c:
                p = textdir / f"cap{i:02d}.txt"
                p.write_text(str(c["text"]), encoding="utf-8")
                c["textfile"] = str(p)
    parts: list[str] = []
    label = "0:v"
    for i, b in enumerate(blurs):
        r = Rect(**b["rect"]).clamped(W, H)
        enable = b.get("enable") or f"between(t,{float(b['t0']):.3f},{float(b['t1']):.3f})"
        nxt = f"b{i}"
        parts += _region_chain(label, nxt, r, b.get("mode", "blur"),
                               b.get("sigma", 0), enable)
        label = nxt
    for i, bx in enumerate(boxes or []):
        r = Rect(**bx["rect"]).clamped(W, H)
        nxt = f"x{i}"
        enable = f":enable='{bx['enable']}'" if bx.get("enable") else ""
        parts.append(f"[{label}]drawbox=x={r.x}:y={r.y}:w={r.w}:h={r.h}"
                     f":color={bx.get('color', 'white@0.85')}:t=fill{enable}[{nxt}]")
        label = nxt
    for i, c in enumerate(captions):
        nxt = f"c{i}"
        parts.append(f"[{label}]{_drawtext(c, W, H, position)}[{nxt}]")
        label = nxt
    if not parts:
        raise FFError("empty filtergraph - nothing to render")
    return ";".join(parts), label


def render(video: Path, out: Path, graph: str | tuple, chapters: list[dict] | None = None,
           vtt: Path | None = None, crf: int = 22, preset: str = "veryfast",
           fps: float | None = None, progress: Path | None = None) -> Path:
    """`progress` gets ffmpeg's machine-readable stats so the dashboard can show a real ETA."""
    graph, label = graph if isinstance(graph, tuple) else (graph, "v")
    out.parent.mkdir(parents=True, exist_ok=True)
    script = out.with_name(f"{out.stem}.filter.txt")
    script.write_text(graph)
    args = [_exe("ffmpeg"), "-hide_banner", "-i", video]
    meta = None
    if chapters:
        meta = out.with_name("chapters.ffmetadata")
        meta.write_text(ffmetadata(chapters))
        args += ["-i", meta]
    args += ["-filter_complex_script", script, "-map", f"[{label}]"]
    if chapters:
        args += ["-map_chapters", "1"]
    if fps:
        args += ["-r", str(fps)]
    if progress:
        args += ["-nostats", "-progress", str(progress), "-loglevel", "error"]
    args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", "-y", out]
    try:
        run(args, timeout=7200)
    finally:
        script.unlink(missing_ok=True)
        if meta:
            meta.unlink(missing_ok=True)
        if progress:
            Path(progress).write_text("progress=end\n")
    if vtt and chapters:
        vtt.parent.mkdir(parents=True, exist_ok=True)
        vtt.write_text(webvtt(chapters))
    return out


def ffmetadata(chapters: list[dict]) -> str:
    lines = [";FFMETADATA1"]
    for c in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(float(c['t0']) * 1000))}",
                  f"END={int(round(float(c['t1']) * 1000))}",
                  f"title={str(c['title']).replace(chr(10), ' ')}"]
    return "\n".join(lines) + "\n"


def webvtt(chapters: list[dict]) -> str:
    def stamp(s: float) -> str:
        return (f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:"
                f"{int(s % 60):02d}.{int(s % 1 * 1000):03d}")

    out = ["WEBVTT", ""]
    for i, c in enumerate(chapters, 1):
        out += [f"{i:03d}", f"{stamp(float(c['t0']))} --> {stamp(float(c['t1']))}",
                str(c["title"]).replace("\n", " "), ""]
    return "\n".join(out)
