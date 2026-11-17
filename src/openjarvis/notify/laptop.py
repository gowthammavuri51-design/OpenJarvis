"""Control the laptop from text messages — a fixed list of approved actions.

Nothing typed in a message is ever passed to a shell: every action maps to
a fixed command line, and "open <app>" only opens apps on the owner's
allow-list (``[notify.laptop].apps``). Shutdown, restart and sleep ask for a
"yes" first.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str]], Tuple[bool, str]]

DEFAULT_APPS: Dict[str, str] = {
    "chrome": "chrome",
    "edge": "msedge",
    "notepad": "notepad",
    "files": "explorer",
    "explorer": "explorer",
    "calculator": "calc",
    "settings": "ms-settings:",
    "task manager": "taskmgr",
}

_FILLER = re.compile(r"\b(please|jarvis|can you|could you|the|my)\b", re.I)
_DEVICE = r"(?:\s+(?:laptop|pc|computer|system|screen))?"
_RE_LOCK = re.compile(rf"^lock{_DEVICE}$")
_RE_SLEEP = re.compile(rf"^(?:sleep|hibernate|put{_DEVICE} to sleep){_DEVICE}$")
_RE_SHUTDOWN = re.compile(
    rf"^(?:shut\s*down|turn\s+off|power\s+off){_DEVICE}(?:\s+(?P<when>(?:in|at)\s+.+))?$"
)
_RE_RESTART = re.compile(rf"^(?:restart|reboot){_DEVICE}$")
_RE_CANCEL = re.compile(r"^(?:cancel|abort|stop)\s+(?:shut\s*down|restart|reboot)$")
_RE_OPEN = re.compile(r"^(?:open|launch|start)\s+(?P<app>.+)$")
_RE_VOLUME = re.compile(r"^(?:volume\s+(?P<dir>up|down)|(?P<mute>mute|unmute))$")
_RE_SCREEN = re.compile(r"^(?:screen\s*shot|show\s+(?:me\s+)?screen|screen)$")
_RE_STATUS = re.compile(r"^(?:(?:laptop|pc|computer)\s+status|status)$")


def _default_runner(argv: Sequence[str]) -> Tuple[bool, str]:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, out


@dataclass
class Plan:
    """A parsed laptop command, ready to run."""

    describe: str  # what will happen, for the confirmation question
    run: Callable[[], "Outcome"]
    confirm: bool = False


@dataclass
class Outcome:
    text: str
    image: Optional[bytes] = None
    ok: bool = True


@dataclass
class LaptopControl:
    apps: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_APPS))
    runner: Runner = _default_runner
    platform: str = sys.platform
    screenshot: Optional[Callable[[], bytes]] = None
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now().astimezone()
    )
    psutil_mod: object = None

    # -------------------------------------------------------------- parsing

    def parse(self, message: str) -> Optional[Plan]:
        text = " ".join(_FILLER.sub(" ", (message or "").lower()).split())
        text = text.rstrip(".!?")
        if not text:
            return None
        if _RE_LOCK.match(text):
            return Plan("lock the laptop", self._lock)
        if _RE_SLEEP.match(text):
            return Plan("put the laptop to sleep", self._sleep, confirm=True)
        m = _RE_SHUTDOWN.match(text)
        if m:
            seconds = self._delay(m.group("when"))
            if isinstance(seconds, str):
                return Plan(seconds, lambda msg=seconds: Outcome(msg, ok=False))
            when = _fmt_delay(seconds)
            return Plan(
                f"shut down the laptop {when}",
                lambda s=seconds: self._shutdown(s),
                confirm=True,
            )
        if _RE_RESTART.match(text):
            return Plan("restart the laptop in 1 minute", self._restart, confirm=True)
        if _RE_CANCEL.match(text):
            return Plan("cancel the shutdown", self._cancel)
        m = _RE_OPEN.match(text)
        if m:
            return Plan(
                f"open {m.group('app')}", lambda a=m.group("app"): self._open(a)
            )
        m = _RE_VOLUME.match(text)
        if m:
            what = m.group("dir") or m.group("mute")
            return Plan(f"volume {what}", lambda w=what: self._volume(w))
        if _RE_SCREEN.match(text):
            return Plan("take a screenshot", self._screenshot)
        if _RE_STATUS.match(text):
            return Plan("laptop status", self._status)
        return None

    def _delay(self, phrase: Optional[str]) -> "int | str":
        if not phrase:
            return 60
        from openjarvis.notify.reminders import parse_when

        try:
            due = parse_when(phrase, self.now()).due
        except ValueError:
            return (
                "I didn't get the time. Try 'shutdown in 30 min' or 'shutdown at 11pm'."
            )
        seconds = int((due - self.now()).total_seconds())
        return max(seconds, 30)

    # -------------------------------------------------------------- actions

    @property
    def _win(self) -> bool:
        return self.platform == "win32"

    def _run(self, argv: Sequence[str], done: str) -> Outcome:
        ok, out = self.runner(argv)
        if ok:
            return Outcome(done)
        return Outcome(f"That didn't work: {out or 'unknown error'}", ok=False)

    def _unsupported(self) -> Outcome:
        return Outcome("Sorry, that only works on Windows for now.", ok=False)

    def _lock(self) -> Outcome:
        if self._win:
            return self._run(["rundll32.exe", "user32.dll,LockWorkStation"], "Locked.")
        if self.platform == "darwin":
            return self._run(["pmset", "displaysleepnow"], "Locked.")
        return self._run(["loginctl", "lock-session"], "Locked.")

    def _sleep(self) -> Outcome:
        if self._win:
            return self._run(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                "Going to sleep. I can't reply until the laptop wakes up.",
            )
        return self._run(["systemctl", "suspend"], "Going to sleep.")

    def _shutdown(self, seconds: int) -> Outcome:
        when = _fmt_delay(seconds)
        msg = f"The laptop will shut down {when}. Send 'cancel shutdown' to stop it."
        if self._win:
            return self._run(["shutdown", "/s", "/t", str(seconds)], msg)
        minutes = max(1, round(seconds / 60))
        return self._run(["shutdown", "-h", f"+{minutes}"], msg)

    def _restart(self) -> Outcome:
        msg = "Restarting in 1 minute. Send 'cancel restart' to stop it."
        if self._win:
            return self._run(["shutdown", "/r", "/t", "60"], msg)
        return self._run(["shutdown", "-r", "+1"], msg)

    def _cancel(self) -> Outcome:
        if self._win:
            return self._run(["shutdown", "/a"], "Cancelled. The laptop stays on.")
        return self._run(["shutdown", "-c"], "Cancelled. The laptop stays on.")

    def _open(self, name: str) -> Outcome:
        apps = {k.lower(): v for k, v in self.apps.items()}
        target = apps.get(name.strip().lower())
        if not target:
            allowed = ", ".join(sorted(apps)) or "(none)"
            return Outcome(
                f"I can only open apps on your list: {allowed}. "
                "Add more under [notify.laptop.apps] in config.toml.",
                ok=False,
            )
        if self._win:
            # `start` resolves app names, paths and protocols like ms-settings:.
            return self._run(["cmd", "/c", "start", "", target], f"Opening {name}.")
        opener = "open" if self.platform == "darwin" else "xdg-open"
        return self._run([opener, target], f"Opening {name}.")

    def _volume(self, what: str) -> Outcome:
        if not self._win:
            return self._unsupported()
        key, times, done = {
            "up": (175, 5, "Volume up."),
            "down": (174, 5, "Volume down."),
            "mute": (173, 1, "Mute toggled."),
            "unmute": (173, 1, "Mute toggled."),
        }[what]
        script = (
            "$w = New-Object -ComObject WScript.Shell; "
            f"1..{times} | ForEach-Object {{ $w.SendKeys([char]{key}) }}"
        )
        return self._run(["powershell", "-NoProfile", "-Command", script], done)

    def _screenshot(self) -> Outcome:
        try:
            png = (self.screenshot or _grab_screen)()
        except Exception as exc:
            return Outcome(f"Couldn't take a screenshot: {exc}", ok=False)
        return Outcome("Here's your screen.", image=png)

    def _status(self) -> Outcome:
        ps = self.psutil_mod
        if ps is None:
            import psutil as ps  # type: ignore[no-redef]
        lines = []
        batt = getattr(ps, "sensors_battery", lambda: None)()
        if batt is not None:
            plug = "charging" if batt.power_plugged else "on battery"
            lines.append(f"Battery: {batt.percent:.0f}% ({plug})")
        drive = "C:\\" if self._win else "/"
        try:
            du = ps.disk_usage(drive)
            lines.append(
                f"Disk {drive}: {du.free / 1024**3:.1f} GB free "
                f"({100 - du.percent:.0f}%)"
            )
        except OSError:
            pass
        mem = ps.virtual_memory()
        lines.append(f"Memory: {mem.percent:.0f}% used")
        lines.append(f"CPU: {ps.cpu_percent(interval=0.5):.0f}%")
        up = time.time() - ps.boot_time()
        lines.append(f"On for: {int(up // 3600)}h {int(up % 3600 // 60)}m")
        return Outcome("\n".join(lines))


def _fmt_delay(seconds: int) -> str:
    if seconds < 90:
        return "in 1 minute"
    if seconds < 3600:
        return f"in {round(seconds / 60)} minutes"
    hours, mins = divmod(round(seconds / 60), 60)
    return f"in {hours}h {mins}m" if mins else f"in {hours}h"


def _grab_screen() -> bytes:
    try:
        from PIL import ImageGrab
    except ImportError:
        if sys.platform == "win32":
            return _grab_screen_powershell()
        raise RuntimeError(
            "screenshots need Pillow: run `uv pip install pillow`"
        ) from None
    import io

    img = ImageGrab.grab(all_screens=True)
    img.thumbnail((1920, 1920))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# Built into Windows: no extra Python packages needed. The output path is
# created here (never user input) and passed as a single-quoted literal.
_PS_SCREENSHOT = """
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$sig = '[DllImport("user32.dll")] ' +
  'public static extern bool SetProcessDPIAware();'
Add-Type -Namespace W -Name U -MemberDefinition $sig
[W.U]::SetProcessDPIAware() | Out-Null
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


def _grab_screen_powershell(runner: Optional[Runner] = None) -> bytes:
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="jarvis-screen-", suffix=".png")
    os.close(fd)
    try:
        script = _PS_SCREENSHOT.replace("{path}", path.replace("'", "''"))
        ok, out = (runner or _default_runner)(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        )
        if not ok:
            raise RuntimeError(out or "PowerShell screenshot failed")
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            raise RuntimeError("the screenshot came out empty")
        return data
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def help_lines() -> List[str]:
    return [
        "- lock laptop / sleep laptop",
        "- shutdown in 30 min / shutdown at 11pm / restart / cancel shutdown",
        "- open chrome (apps on your list)",
        "- volume up / volume down / mute",
        "- screenshot / laptop status",
    ]
