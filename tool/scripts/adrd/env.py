"""Locate adb, ffmpeg, flutter and the fonts the render pipeline depends on."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
SDK_CANDIDATES = [
    os.environ.get("ANDROID_HOME"),
    os.environ.get("ANDROID_SDK_ROOT"),
    str(HOME / "Library/Android/sdk"),
    str(HOME / "Android/Sdk"),
    str(HOME / "AppData/Local/Android/Sdk"),
]


def _first_existing(paths) -> Path | None:
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def find_adb() -> Path | None:
    which = shutil.which("adb")
    if which:
        return Path(which)
    return _first_existing(
        f"{sdk}/platform-tools/adb" for sdk in SDK_CANDIDATES if sdk
    )


def find_emulator() -> Path | None:
    which = shutil.which("emulator")
    if which:
        return Path(which)
    return _first_existing(f"{sdk}/emulator/emulator" for sdk in SDK_CANDIDATES if sdk)


def find_flutter() -> Path | None:
    which = shutil.which("flutter")
    if which:
        return Path(which)
    return _first_existing(
        [
            str(HOME / "development/flutter/bin/flutter"),
            str(HOME / "flutter/bin/flutter"),
            "/opt/homebrew/bin/flutter",
        ]
    )


def find_ffmpeg() -> Path | None:
    which = shutil.which("ffmpeg")
    if which:
        return Path(which)
    try:
        import imageio_ffmpeg

        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def find_ffprobe() -> Path | None:
    which = shutil.which("ffprobe")
    if which:
        return Path(which)
    ff = find_ffmpeg()
    if ff and ff.name.startswith("ffmpeg-"):
        cand = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
        if cand.exists():
            return cand
    return None


def find_font() -> Path:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for p in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        windir / "Fonts/arial.ttf",
        windir / "Fonts/segoeui.ttf",
        windir / "Fonts/calibri.ttf",
    ):
        if Path(p).exists():
            return Path(p)
    raise FileNotFoundError("no truetype font found for drawtext")


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def cf_dir() -> Path:
    """Where cloudflared keeps the account cert and per-tunnel credentials."""
    return Path.home() / ".cloudflared"


def cf_cert() -> Path:
    return cf_dir() / "cert.pem"


def doctor() -> list[Probe]:
    probes: list[Probe] = []

    adb = find_adb()
    probes.append(
        Probe("adb", adb is not None, str(adb) if adb else "not found",
              "install Android SDK platform-tools")
    )
    ff = find_ffmpeg()
    ok_ff = ff is not None
    if ok_ff:
        out = subprocess.run([str(ff), "-hide_banner", "-version"],
                             capture_output=True, text=True).stdout
        ok_ff = "ffmpeg version" in out
        detail = out.splitlines()[0] if ok_ff else "unusable binary"
    else:
        detail = "not found"
    probes.append(
        Probe("ffmpeg", ok_ff, detail,
              "python3 -m pip install imageio-ffmpeg pillow")
    )

    drawtext = False
    if ok_ff:
        filters = subprocess.run([str(ff), "-hide_banner", "-filters"],
                                 capture_output=True, text=True).stdout
        drawtext = all(k in filters for k in ("drawtext", "gblur", "scdet"))
    probes.append(
        Probe("ffmpeg filters", drawtext,
              "drawtext + gblur + scdet" if drawtext else "missing required filters",
              "a static build without libfreetype cannot burn captions")
    )

    try:
        import PIL  # noqa: F401

        probes.append(Probe("pillow", True, PIL.__version__))
    except Exception as exc:
        probes.append(Probe("pillow", False, str(exc),
                            "python3 -m pip install pillow"))

    font = None
    try:
        font = find_font()
    except FileNotFoundError:
        pass
    probes.append(Probe("caption font", font is not None, str(font) or "not found"))

    fl = find_flutter()
    probes.append(Probe("flutter", fl is not None, str(fl) if fl else "not found",
                        "only needed when the demo APK must be built"))

    wk = shutil.which("wkhtmltopdf")
    probes.append(Probe("wkhtmltopdf", wk is not None, str(wk) if wk else "not found",
                        "only needed for the A4 report; SESSION_DOC.md is complete without it"))

    cfl = shutil.which("cloudflared")
    signed = cfl is not None and cf_cert().exists()
    probes.append(Probe("cloudflared", cfl is not None, str(cfl) if cfl else "not found",
                        "brew install cloudflared"))
    probes.append(Probe("cloudflare login", signed,
                        str(cf_cert()) if signed else "no cert - quick tunnels only",
                        "adrd tunnel --session DIR --login   (once, in the browser) - "
                        "without it a fixed hostname cannot be bound"))

    serials = list_serials() if adb else []
    emu = [s for s in serials if "-emu" in s or s.startswith("emulator-")]
    usb = [s for s in serials if s not in emu]
    probes.append(
        Probe("device", bool(serials), ", ".join(serials) or "none attached",
              "attach the phone with USB debugging, or: emulator -avd <name>")
    )
    if not emu:
        probes.append(
            Probe("avd", False, "no virtual device",
                  "avdmanager create avd -n demo -k <system-image-id>")
        )
    return probes


def list_serials() -> list[str]:
    adb = find_adb()
    if not adb:
        return []
    out = subprocess.run([str(adb), "devices"], capture_output=True,
                         text=True).stdout
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def pick_serial(serial: str | None = None) -> str:
    if serial:
        return serial
    serials = list_serials()
    if not serials:
        raise RuntimeError("no device attached; run `adrd doctor`")
    emu = [s for s in serials if s.startswith("emulator-")]
    return (emu or serials)[0]
