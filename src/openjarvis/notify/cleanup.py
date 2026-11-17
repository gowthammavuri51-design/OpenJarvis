"""Free disk space safely: scan first, delete only what the owner approves.

Only well-known *junk* locations are ever deleted (temp folders, browser
caches, crash dumps, package-manager download caches, the Recycle Bin).
Personal folders (Documents, Downloads, Desktop, Pictures, Videos, Music)
are never deleted from; their biggest files are only *listed* so the owner
can decide.

Safety rules applied to every deletion:

* only files *inside* a category root are removed, never the root itself;
* symlinks / junctions / reparse points are never followed or deleted;
* temp files younger than ``min_age_hours`` are kept (running installers);
* files that are in use (locked) are skipped silently.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PERSONAL = ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music")


@dataclass
class Category:
    key: str
    name: str
    roots: List[str] = field(default_factory=list)
    min_age_hours: float = 0.0
    special: str = ""  # "recycle_bin" for the Windows Recycle Bin

    # filled by scan()
    size: int = 0
    files: int = 0


@dataclass
class BigFile:
    path: str
    size: int


@dataclass
class ScanResult:
    categories: List[Category]
    big_files: List[BigFile]
    scanned_at: float
    drive: str
    free_bytes: int
    total_bytes: int

    @property
    def reclaimable(self) -> int:
        return sum(c.size for c in self.categories)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "ScanResult":
        d = json.loads(raw)
        return cls(
            categories=[Category(**c) for c in d["categories"]],
            big_files=[BigFile(**b) for b in d["big_files"]],
            scanned_at=d["scanned_at"],
            drive=d["drive"],
            free_bytes=d["free_bytes"],
            total_bytes=d["total_bytes"],
        )


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"  # pragma: no cover


# ----------------------------------------------------------------- locations


def _expand(patterns: Iterable[str]) -> List[str]:
    out: List[str] = []
    for pat in patterns:
        pat = os.path.expandvars(os.path.expanduser(pat))
        if "%" in pat or "$" in pat:  # unexpanded variable: skip
            continue
        for p in glob.glob(pat):
            if os.path.isdir(p) and not _is_link(p):
                out.append(os.path.abspath(p))
    return sorted(set(out))


def default_categories() -> List[Category]:
    """Junk locations for this OS."""
    if sys.platform == "win32":
        browsers = []
        for vendor in (
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
            r"%LOCALAPPDATA%\Google\Chrome\User Data",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
        ):
            for sub in ("Cache", "Code Cache", "GPUCache"):
                browsers.append(rf"{vendor}\*\{sub}")
        browsers.append(r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2")
        return [
            Category("temp", "Temporary files", [r"%TEMP%", r"%SystemRoot%\Temp"], 24),
            Category("browser", "Browser caches", browsers),
            Category(
                "crash",
                "Crash dumps & error reports",
                [
                    r"%LOCALAPPDATA%\CrashDumps",
                    r"%SystemRoot%\Minidump",
                    r"%ProgramData%\Microsoft\Windows\WER\ReportArchive",
                    r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportArchive",
                ],
            ),
            Category(
                "packages",
                "Download caches (pip, npm, Windows Update)",
                [
                    r"%LOCALAPPDATA%\pip\cache",
                    r"%LOCALAPPDATA%\npm-cache",
                    r"%SystemRoot%\SoftwareDistribution\Download",
                ],
                24,
            ),
            Category("recycle", "Recycle Bin", [], special="recycle_bin"),
        ]
    home = "~"
    caches = "~/Library/Caches" if sys.platform == "darwin" else "~/.cache"
    return [
        Category("temp", "Temporary files", ["/tmp", "/var/tmp"], 24),
        Category(
            "browser",
            "Browser caches",
            [
                f"{caches}/google-chrome",
                f"{caches}/chromium",
                f"{caches}/mozilla/firefox/*/cache2",
            ],
        ),
        Category(
            "packages",
            "Download caches (pip, npm)",
            [f"{caches}/pip", f"{home}/.npm/_cacache"],
            24,
        ),
        Category("recycle", "Trash", [f"{home}/.local/share/Trash/files"]),
    ]


def personal_folders() -> List[str]:
    home = Path.home()
    return [str(home / name) for name in _PERSONAL if (home / name).is_dir()]


def system_drive() -> str:
    if sys.platform == "win32":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


# ----------------------------------------------------------------- walking


def _is_link(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _iter_files(root: str, min_age_s: float, now: float) -> Iterator[os.DirEntry]:
    """Yield regular files under root, never following links/junctions."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or (
                getattr(st, "st_file_attributes", 0) & _REPARSE
            ):
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(e.path)
            elif stat.S_ISREG(st.st_mode):
                if min_age_s and now - st.st_mtime < min_age_s:
                    continue
                yield e


def _inside(path: str, root: str) -> bool:
    try:
        rp, rr = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([rp, rr]) == rr and rp != rr
    except ValueError:  # different drives on Windows
        return False


# ----------------------------------------------------------------- recycle bin


def _recycle_bin_size() -> tuple[int, int]:
    if sys.platform != "win32":  # pragma: no cover - Windows only
        return 0, 0
    import ctypes
    from ctypes import wintypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("i64Size", ctypes.c_longlong),
            ("i64NumItems", ctypes.c_longlong),
        ]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def _empty_recycle_bin() -> int:
    if sys.platform != "win32":  # pragma: no cover - Windows only
        return 0
    import ctypes

    size, _ = _recycle_bin_size()
    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
    after, _ = _recycle_bin_size()
    return max(0, size - after)


# ----------------------------------------------------------------- scan / clean


def scan(
    categories: Optional[List[Category]] = None,
    personal: Optional[List[str]] = None,
    *,
    big_file_min: int = 200 * 1024**2,
    big_file_count: int = 5,
    drive: Optional[str] = None,
    now: Optional[float] = None,
    disk_usage: Optional[Callable[[str], object]] = None,
) -> ScanResult:
    now = now or time.time()
    cats = categories if categories is not None else default_categories()
    for c in cats:
        c.size = c.files = 0
        if c.special == "recycle_bin":
            c.size, c.files = _recycle_bin_size()
            continue
        c.roots = _expand(c.roots)
        for root in c.roots:
            for e in _iter_files(root, c.min_age_hours * 3600, now):
                try:
                    c.size += e.stat(follow_symlinks=False).st_size
                    c.files += 1
                except OSError:
                    pass

    big: List[BigFile] = []
    for folder in personal if personal is not None else personal_folders():
        for e in _iter_files(folder, 0, now):
            try:
                size = e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if size >= big_file_min:
                big.append(BigFile(e.path, size))
    big.sort(key=lambda b: b.size, reverse=True)

    drive = drive or system_drive()
    if disk_usage is None:
        import shutil

        disk_usage = shutil.disk_usage
    try:
        du = disk_usage(drive)
        free, total = int(du.free), int(du.total)
    except OSError:
        free = total = 0
    return ScanResult(
        categories=[c for c in cats if c.size > 0],
        big_files=big[:big_file_count],
        scanned_at=now,
        drive=drive,
        free_bytes=free,
        total_bytes=total,
    )


@dataclass
class CleanResult:
    freed: int = 0
    deleted: int = 0
    skipped: int = 0
    by_category: Dict[str, int] = field(default_factory=dict)


def clean(
    categories: List[Category],
    *,
    now: Optional[float] = None,
) -> CleanResult:
    """Delete junk in the given (scanned) categories. Personal files never."""
    now = now or time.time()
    res = CleanResult()
    for c in categories:
        freed_before = res.freed
        if c.special == "recycle_bin":
            try:
                res.freed += _empty_recycle_bin()
            except Exception as exc:
                logger.warning("Emptying the Recycle Bin failed: %s", exc)
            res.by_category[c.name] = res.freed - freed_before
            continue
        for root in c.roots:
            for e in _iter_files(root, c.min_age_hours * 3600, now):
                if not _inside(e.path, root):
                    res.skipped += 1
                    continue
                try:
                    size = e.stat(follow_symlinks=False).st_size
                    os.remove(e.path)
                except OSError:
                    res.skipped += 1  # in use or no permission
                    continue
                res.freed += size
                res.deleted += 1
            _remove_empty_dirs(root)
        res.by_category[c.name] = res.freed - freed_before
    return res


def _remove_empty_dirs(root: str) -> None:
    for dirpath, dirnames, _ in os.walk(root, topdown=False, followlinks=False):
        if os.path.realpath(dirpath) == os.path.realpath(root):
            continue
        if _is_link(dirpath):
            continue
        try:
            os.rmdir(dirpath)  # only succeeds when empty
        except OSError:
            pass


# ----------------------------------------------------------------- text


def format_scan(r: ScanResult) -> str:
    lines = []
    if r.total_bytes:
        pct = 100 * r.free_bytes / r.total_bytes
        lines.append(f"Drive {r.drive} has {human(r.free_bytes)} free ({pct:.0f}%).")
    if r.categories:
        lines.append(f"I can safely free about {human(r.reclaimable)}:")
        for i, c in enumerate(r.categories, 1):
            lines.append(f"{i}. {c.name}: {human(c.size)}")
        lines.append("Reply 'clean all' or e.g. 'clean 1 3'.")
    else:
        lines.append("No junk files worth cleaning right now.")
    if r.big_files:
        lines.append("")
        lines.append("Your biggest files (I won't delete these, check them yourself):")
        for b in r.big_files:
            lines.append(f"- {human(b.size)}  {b.path}")
    return "\n".join(lines)


def format_clean(res: CleanResult, free_after: Optional[int] = None) -> str:
    lines = [f"Done. Freed {human(res.freed)} ({res.deleted} files)."]
    for name, freed in res.by_category.items():
        lines.append(f"- {name}: {human(freed)}")
    if res.skipped:
        lines.append(f"Skipped {res.skipped} files that were in use.")
    if free_after is not None:
        lines.append(f"Free space now: {human(free_after)}.")
    return "\n".join(lines)


def select(r: ScanResult, choice: str) -> List[Category]:
    """Pick categories from 'all' or numbers like '1 3' / '1,3'."""
    choice = choice.strip().lower()
    if choice in ("all", "everything", "yes"):
        return list(r.categories)
    picked: List[Category] = []
    for token in choice.replace(",", " ").split():
        if not token.isdigit():
            raise ValueError(f"'{token}' is not a number from the list.")
        idx = int(token)
        if not 1 <= idx <= len(r.categories):
            raise ValueError(f"There is no item {idx} in the list.")
        cat = r.categories[idx - 1]
        if cat not in picked:
            picked.append(cat)
    if not picked:
        raise ValueError("Say 'clean all' or list numbers, e.g. 'clean 1 3'.")
    return picked
