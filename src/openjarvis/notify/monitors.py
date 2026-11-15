"""Laptop health checks (Windows, macOS, Linux) using psutil."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from openjarvis.notify.config import MonitorConfig


@dataclass
class Finding:
    key: str  # dedupe key, e.g. "disk:C:\\"
    urgency: str
    title: str
    message: str


def _psutil() -> Any:
    import psutil

    return psutil


_SKIP_FSTYPES = frozenset(
    {"squashfs", "iso9660", "udf", "tmpfs", "devtmpfs", "overlay", "cdfs"}
)
_MIN_DISK_BYTES = 1024**3


def check_disks(cfg: MonitorConfig, ps: Any = None) -> List[Finding]:
    ps = ps or _psutil()
    out: List[Finding] = []
    threshold = float(cfg.disk_free_percent)
    if threshold <= 0:
        return out
    for part in ps.disk_partitions(all=False):
        opts = {o.strip() for o in (part.opts or "").lower().split(",")}
        fstype = (part.fstype or "").lower()
        # Skip CD drives, removable drives, read-only and pseudo filesystems:
        # they are often "full" by design and the user can't free space there.
        if not fstype or fstype in _SKIP_FSTYPES or opts & {"cdrom", "removable", "ro"}:
            continue
        try:
            usage = ps.disk_usage(part.mountpoint)
        except (OSError, PermissionError):
            continue
        if usage.total < _MIN_DISK_BYTES:
            continue
        free_pct = 100.0 - float(usage.percent)
        if free_pct < threshold:
            free_gb = usage.free / 1024**3
            urgency = "high" if free_pct < threshold / 2 else "normal"
            out.append(
                Finding(
                    key=f"disk:{part.mountpoint}",
                    urgency=urgency,
                    title=f"Disk {part.mountpoint} almost full",
                    message=f"Only {free_pct:.0f}% ({free_gb:.1f} GB) free.",
                )
            )
    return out


def check_battery(cfg: MonitorConfig, ps: Any = None) -> List[Finding]:
    ps = ps or _psutil()
    threshold = float(cfg.battery_percent)
    getter = getattr(ps, "sensors_battery", None)
    if threshold <= 0 or getter is None:
        return []
    try:
        batt = getter()
    except Exception:
        return []
    if batt is None or batt.power_plugged or batt.percent > threshold:
        return []
    urgency = "high" if batt.percent <= max(5.0, threshold / 2) else "normal"
    return [
        Finding(
            key="battery:low",
            urgency=urgency,
            title="Laptop battery low",
            message=f"Battery at {batt.percent:.0f}% and not charging. Plug it in.",
        )
    ]


def check_memory(cfg: MonitorConfig, ps: Any = None) -> List[Finding]:
    ps = ps or _psutil()
    threshold = float(cfg.memory_percent)
    if threshold <= 0:
        return []
    mem = ps.virtual_memory()
    if mem.percent < threshold:
        return []
    top = ""
    try:
        procs = sorted(
            ps.process_iter(["name", "memory_percent"]),
            key=lambda p: p.info.get("memory_percent") or 0,
            reverse=True,
        )[:3]
        names = [f"{p.info['name']} ({p.info['memory_percent']:.0f}%)" for p in procs]
        top = " Top: " + ", ".join(names) + "."
    except Exception:
        pass
    return [
        Finding(
            key="memory:high",
            urgency="normal",
            title="Laptop memory nearly full",
            message=f"Memory use at {mem.percent:.0f}%.{top}",
        )
    ]


def check_processes(cfg: MonitorConfig, ps: Any = None) -> List[Finding]:
    wanted = [p for p in (cfg.watch_processes or []) if p]
    if not wanted:
        return []
    ps = ps or _psutil()
    running = set()
    for p in ps.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        if name:
            running.add(name)
    out = []
    for name in wanted:
        if name.lower() not in running:
            out.append(
                Finding(
                    key=f"process:{name.lower()}",
                    urgency="normal",
                    title=f"{name} is not running",
                    message=f"{name} stopped or crashed on your laptop.",
                )
            )
    return out


CHECKS: List[Callable[..., List[Finding]]] = [
    check_disks,
    check_battery,
    check_memory,
    check_processes,
]


def run_all(cfg: MonitorConfig, ps: Optional[Any] = None) -> List[Finding]:
    findings: List[Finding] = []
    for check in CHECKS:
        try:
            findings.extend(check(cfg, ps))
        except Exception as exc:  # one bad check must not stop the others
            findings.append(
                Finding(
                    key=f"monitor-error:{check.__name__}",
                    urgency="low",
                    title=f"Health check {check.__name__} failed",
                    message=str(exc)[:200],
                )
            )
    return findings
