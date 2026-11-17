"""Background loop: run health checks, escalate, flush held alerts, send digest."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from openjarvis.notify import monitors
from openjarvis.notify.notifier import Notifier, NotifyResult

logger = logging.getLogger(__name__)


@dataclass
class TickReport:
    findings: List[NotifyResult] = field(default_factory=list)
    recalled: List[int] = field(default_factory=list)
    flushed: int = 0
    digested: int = 0


def _poll_replies(notifier: Notifier) -> None:
    try:
        notifier.poll_telegram_replies()
    except Exception as exc:
        logger.warning("Reading Telegram replies failed: %s", exc)


def tick(notifier: Notifier, ps: Optional[object] = None) -> TickReport:
    report = TickReport()
    _poll_replies(notifier)
    for f in monitors.run_all(notifier.cfg.monitors, ps):
        report.findings.append(
            notifier.notify(f.title, f.message, urgency=f.urgency, dedupe_key=f.key)
        )
    report.recalled = notifier.process_escalations()
    report.flushed = notifier.flush_held()
    report.digested = notifier.send_digest_if_due()
    return report


def run_forever(notifier: Notifier, stop: Optional[threading.Event] = None) -> None:
    stop = stop or threading.Event()
    interval = max(30, int(notifier.cfg.check_interval_seconds))
    # Replies and escalations need finer timing than health checks.
    step = 15
    elapsed = interval  # run health checks immediately
    while not stop.is_set():
        try:
            if elapsed >= interval:
                tick(notifier)
                elapsed = 0
            else:
                _poll_replies(notifier)
                notifier.process_escalations()
        except Exception:
            logger.exception("notify watcher tick failed")
        stop.wait(step)
        elapsed += step
