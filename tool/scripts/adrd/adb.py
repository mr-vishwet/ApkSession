"""Device control: screencap, input synthesis, launch, and segmented recording."""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .env import find_adb, pick_serial

REMOTE_DIR = "/data/local/tmp/adrd"
MAX_SEGMENT_S = 1740  # screenrecord hard-caps at 1800; leave headroom


class AdbError(RuntimeError):
    pass


@dataclass
class Adb:
    serial: str | None = None
    _serial: str = field(init=False, default="")
    _bin: str = field(init=False, default="")

    def __post_init__(self):
        adb = find_adb()
        if not adb:
            raise AdbError("adb not found; install Android platform-tools")
        self._bin = str(adb)
        self._serial = pick_serial(self.serial)

    @property
    def device(self) -> str:
        return self._serial

    def argv(self, *args: str) -> list[str]:
        return [self._bin, "-s", self._serial, *args]

    def run(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(self.argv(*args), capture_output=True, text=True,
                              timeout=timeout)

    def shell(self, cmd: str, timeout: float = 60) -> str:
        cp = self.run("shell", cmd, timeout=timeout)
        if cp.returncode != 0:
            raise AdbError(f"adb shell {cmd!r}: {cp.stderr.strip() or cp.stdout.strip()}")
        return cp.stdout

    def raw(self, *args: str, log=None) -> subprocess.Popen:
        # stderr must go somewhere that drains. A PIPE nobody reads deadlocks the
        # child the moment its buffer fills, which stalls screenrecord mid-take.
        sink = subprocess.DEVNULL if log is None else open(log, "a")
        return subprocess.Popen(self.argv(*args), stdout=subprocess.DEVNULL,
                                stderr=sink, text=True)

    # ---- identity -------------------------------------------------------
    def sdk(self) -> int:
        out = self.shell("getprop ro.build.version.sdk")
        m = re.search(r"\d+", out)
        return int(m.group()) if m else 0

    def model(self) -> str:
        return self.shell("getprop ro.product.model").strip()

    def screen_size(self) -> tuple[int, int]:
        for cmd in ("wm size", "dumpsys display | grep -m1 -i 'deviceResolution'"):
            out = self.shell(cmd)
            m = re.search(r"(\d{2,5})x(\d{2,5})", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        raise AdbError(f"cannot read screen size: {out!r}")

    def packages(self) -> list[str]:
        return re.findall(r"package:(\S+)", self.shell("pm list packages"))

    def install(self, apk: Path) -> tuple[bool, str]:
        cp = self.run("install", "-r", "-t", str(apk), timeout=900)
        blob = cp.stdout + cp.stderr
        return ("Success" in blob), blob.strip()

    def launchable(self, pkg: str) -> str:
        out = self.shell(f"cmd package resolve-activity --brief {shlex.quote(pkg)}",
                         timeout=30)
        for line in reversed(out.strip().splitlines()):
            if "/" in line and "package" not in line.lower():
                return line.strip()
        raise AdbError(f"no launchable activity for {pkg}: {out!r}")

    def clear_data(self, pkg: str) -> None:
        """Wipe local app state. The app stays logged in across force-stop, so without
        this a 'cold start' take silently replays a logged-in session."""
        self.shell(f"pm clear {shlex.quote(pkg)}", timeout=90)
        time.sleep(1.0)

    def start_app(self, pkg: str, wait_s: float = 3.0, clear: bool = False) -> str:
        component = self.launchable(pkg)
        if clear:
            self.clear_data(pkg)
        self.shell(f"am force-stop {shlex.quote(pkg)}")
        time.sleep(0.4)
        self.shell(f"am start -n {shlex.quote(component)}")
        time.sleep(wait_s)
        return component

    def current_focus(self) -> str:
        out = self.shell("dumpsys window | grep -m5 -E 'mCurrentFocus|mFocusedApp'")
        m = re.findall(r"(\S+/\S+?)\s*\}", out)
        return m[-1] if m else ""

    # ---- capture --------------------------------------------------------
    def screencap(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run(self.argv("exec-out", "screencap", "-p"),
                            capture_output=True, timeout=60)
        if cp.returncode != 0 or len(cp.stdout) < 1024:
            raise AdbError(
                f"screencap failed ({len(cp.stdout)} bytes). FLAG_SECURE screens "
                f"and some OEM ROMs block capture: {cp.stderr.decode(errors='ignore')}"
            )
        dest.write_bytes(cp.stdout)
        return dest

    # ---- input ----------------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def long_press(self, x: int, y: int, ms: int = 800) -> None:
        self.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(ms)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 260) -> None:
        self.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}"
        )

    def keyevent(self, code: int | str) -> None:
        self.shell(f"input keyevent {code}")

    def type_text(self, text: str) -> None:
        if not text:
            return
        escaped = text.replace(" ", "%s").replace("'", "'\\''")
        self.shell(f"input text '{escaped}'")


class Recorder:
    """Segments device-side screenrecord so a session can exceed the 30 min cap."""

    def __init__(self, adb: Adb, bit_rate: int = 12_000_000,
                 segment_s: int = MAX_SEGMENT_S):
        self.adb = adb
        self.bit_rate = bit_rate
        self.segment_s = min(int(segment_s), MAX_SEGMENT_S)
        self.segments: list[dict] = []
        self._proc: subprocess.Popen | None = None
        self._index = -1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._remote_base = f"{REMOTE_DIR}/{int(time.time())}"
        self.log: Path | None = None

    def start(self, log: Path | None = None) -> None:
        self.log = log or self.log
        self.adb.shell(f"mkdir -p {self._remote_base}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline and not self.segments:
            time.sleep(0.1)
        if not self.segments:
            raise AdbError("screenrecord did not start; check OEM recording limits")

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._proc is not None and self._proc.poll() is None:
                if time.time() - self.segments[-1]["host_start"] < self.segment_s:
                    time.sleep(0.5)
                    continue
            time.sleep(0.3)
            self._open_segment()

    def _open_segment(self) -> None:
        if self.segments and self.segments[-1]["host_end"] is None:
            self.segments[-1]["host_end"] = time.time()
        self._index += 1
        remote = f"{self._remote_base}/seg{self._index:03d}.mp4"
        host_start = time.time()
        self._proc = self.adb.raw(
            "shell", "screenrecord",
            "--bit-rate", str(self.bit_rate),
            "--time-limit", str(self.segment_s),
            "--verbose", remote, log=self.log,
        )
        self.segments.append({"index": self._index, "remote": remote,
                             "host_start": host_start, "host_end": None,
                             "seconds": None})

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._finalize()

    def _finalize(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            # SIGINT makes screenrecord flush the moov atom; SIGKILL corrupts the file.
            self.adb.shell("pkill -2 -f screenrecord", timeout=20)
            try:
                self._proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                pass
        if self.segments and self.segments[-1]["host_end"] is None:
            self.segments[-1]["host_end"] = time.time()

    def pull(self, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for seg in self.segments:
            local = out_dir / f"raw{seg['index']:03d}.mp4"
            cp = subprocess.run(self.adb.argv("pull", seg["remote"], str(local)),
                                capture_output=True, text=True, timeout=900)
            if cp.returncode != 0 or not local.exists():
                raise AdbError(f"adb pull failed for {seg['remote']}: {cp.stderr}")
            seg["host_end"] = seg["host_end"] or time.time()
            paths.append(local)
        self.adb.shell(f"rm -rf {self._remote_base}", timeout=60)
        return paths
