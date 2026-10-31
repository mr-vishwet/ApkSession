#!/usr/bin/env python3
"""Make a fake 6-screen recording plus the step events a real take would leave behind.

Lets every downstream stage (align, brand, render, proof, sweep, doc) be tested with no
phone attached. Run through run.sh, which asserts the whole chain.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
from adrd import ff  # noqa: E402
from adrd.env import find_ffmpeg  # noqa: E402
from adrd.session import Session  # noqa: E402

W, H = 1080, 2400
SCREENS = ["splash", "login", "signup", "otp", "home", "reports"]
BGS = ["#0b1f3a", "#12233d", "#1b2a4a", "#0f2b3d", "#152238", "#20183a"]
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
HOST_SHIFT = 0.6
WORDMARK = "160,105,560,140"


def make_screen(i: int, out: Path, seconds: float = 10.0) -> Path:
    """One 'screen': app bar wordmark, brand glyph, fields, a CTA. Unique colours."""
    vf = [
        f"drawbox=x=0:y=90:w={W}:h=170:color=0x2b3f5c@1:t=fill",
        f"drawtext=fontfile={FONT}:text='UnionField':fontsize=88:fontcolor=0xf2a53d"
        ":x=180:y=125",
        "drawbox=x=60:y=110:w=110:h=110:color=0xe84c3d@1:t=fill",
        "drawbox=x=85:y=135:w=60:h=60:color=0xffffff@1:t=fill",
        f"drawtext=fontfile={FONT}:text='U':fontsize=52:fontcolor=0xe84c3d:x=98:y=138",
        f"drawtext=fontfile={FONT}:text='{SCREENS[i].upper()}':fontsize=72:"
        "fontcolor=white:x=60:y=360",
        f"drawtext=fontfile={FONT}:text='email field / password / CTA':fontsize=48:"
        "fontcolor=0x9fd3ff:x=60:y=470",
        f"drawbox=x=60:y=700:w=960:h=120:color=0x33506f@1:t=fill",
        f"drawbox=x=60:y=900:w=960:h=120:color=0x33506f@1:t=fill",
        "drawbox=x=300:y=1200:w=480:h=140:color=0x2ecc71@1:t=fill",
        f"drawbox=x=0:y={H - 190}:w={W}:h=190:color=0x1a2740@1:t=fill",
    ]
    subprocess.run([str(find_ffmpeg()), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={BGS[i]}:s={W}x{H}:r=30",
                    "-t", str(seconds), "-vf", ",".join(vf), "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-y", out], check=True, capture_output=True)
    return out


def main() -> int:
    # --compressed mimics Android's screenrecord, which encodes on invalidation: 10
    # wall seconds of dwell can be 3 seconds of file, so host time and video time diverge.
    session, clips = Session(sys.argv[1]), Path(sys.argv[2])
    clip_s = 3.0 if "--compressed" in sys.argv else 10.0
    step_s = 10.0
    clips.mkdir(parents=True, exist_ok=True)
    rec = ff.concat([make_screen(i, clips / f"{i:02d}_{SCREENS[i]}.mp4", clip_s)
                     for i in range(len(SCREENS))], session.path("recording"))
    total = ff.duration(rec)
    events, i = [], 0
    for k, name in enumerate(SCREENS):
        ff.frame(rec, k * 10 + 5, session.stills / f"{name}.png")
        for j, note in enumerate(["screen opens", "fill field", "press primary action"]):
            events.append({"i": i, "chapter": name if j == 0 else None,
                           "label": f"{name}: {note}", "screen": name,
                           "host": k * step_s + 0.4 + j * 3.0 + HOST_SHIFT,
                           "t_raw": k * step_s + 0.4 + j * 3.0 + HOST_SHIFT,
                           "ops": ["tap"] if j else ["wait"], "at": [540, 1270]})
            i += 1
    session.write("events", {"size": [W, H], "segments": [], "events": events}, quiet=True)
    session.update_state(app="fixture-app", package="com.client.fixture",
                         device_model="synthetic", serial="synthetic",
                         screen_size=[W, H], duration=round(total, 3))
    print(f"{total:.1f}s recording, {len(events)} events, clip {clip_s}s, "
          f"step {step_s}s, true clock shift {HOST_SHIFT}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
