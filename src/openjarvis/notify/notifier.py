"""Decide how to reach the owner (call / text / digest) and track acknowledgement.

Routing by urgency
------------------
=========  ==========================================  ===================
urgency    outside quiet hours                         during quiet hours
=========  ==========================================  ===================
low        added to the daily digest text              same
normal     text message                                held, sent after
high       text + phone call                           text only
critical   text + phone call, re-called until acked    same (ignores quiet)
=========  ==========================================  ===================

Non-critical texts/calls are limited to ``max_per_hour``; extra ones are held
and sent together later. Repeats of the same ``dedupe_key`` within
``dedupe_hours`` are dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, List, Optional
from xml.sax.saxutils import escape

from openjarvis.notify import store as st
from openjarvis.notify.config import NotifyConfig
from openjarvis.notify.gateway import Gateway, build_call_twiml

logger = logging.getLogger(__name__)

_SMS_LIMIT = 1500
_SPOKEN_LIMIT = 700
_ACK_RE = re.compile(
    r"^\s*(ok|okay|ack|yes|got it|done)\s*#?\s*(\d+)?\s*[.!]?\s*$", re.I
)


@dataclass
class NotifyResult:
    action: str  # called | texted | held | digest | suppressed | failed
    alert_id: Optional[int] = None
    detail: str = ""


def _in_window(now: time, window: Optional[tuple[time, time]]) -> bool:
    if not window:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # spans midnight


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Notifier:
    def __init__(
        self,
        config: NotifyConfig,
        gateway: Gateway,
        store: Optional[st.AlertStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.cfg = config
        self.gw = gateway
        self.store = store or st.AlertStore()
        self._clock = clock or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        return self._clock()

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        now = now or self._now()
        return _in_window(now.time(), self.cfg.quiet_window)

    def _rate_limited(self, ts: float) -> bool:
        if self.cfg.max_per_hour <= 0:
            return False
        return self.store.contacts_since(ts - 3600) >= self.cfg.max_per_hour

    def _gather_url(self, alert_id: int) -> Optional[str]:
        if not self.cfg.can_receive_webhooks:
            return None
        return f"{self.cfg.public_url}/webhooks/twilio/voice?alert_id={alert_id}"

    def _sms(self, body: str, ts: float, critical: bool) -> bool:
        try:
            self.gw.send_sms(self.cfg.owner_phone, _clip(body, _SMS_LIMIT))
        except Exception as exc:
            logger.error("Notify SMS failed: %s", exc)
            return False
        self.store.log_contact(ts, "sms", critical)
        return True

    def _call(self, alert: st.Alert, ts: float, *, retry: bool = False) -> bool:
        intro = "This is Jarvis calling again" if retry else "Hello, this is Jarvis"
        spoken = _clip(
            f"{intro} with a {alert.urgency} alert. {alert.title}. {alert.message}",
            _SPOKEN_LIMIT,
        )
        twiml = build_call_twiml(
            spoken,
            language=self.cfg.voice_language,
            gather_url=self._gather_url(alert.id),
        )
        try:
            self.gw.place_call(self.cfg.owner_phone, twiml)
        except Exception as exc:
            logger.error("Notify call failed: %s", exc)
            return False
        self.store.log_contact(ts, "call", alert.urgency == "critical")
        self.store.update(alert.id, calls=alert.calls + 1, last_call_at=ts)
        return True

    # ------------------------------------------------------------------ public

    def notify(
        self,
        title: str,
        message: str = "",
        *,
        urgency: str = "normal",
        dedupe_key: str = "",
    ) -> NotifyResult:
        """Route one alert to the owner according to urgency and policy."""
        urgency = (urgency or "normal").lower()
        if urgency not in st.URGENCIES:
            raise ValueError(f"urgency must be one of {st.URGENCIES}")
        title = _clip(title or "Update", 160)
        message = (message or "").strip()
        now = self._now()
        ts = now.timestamp()

        if dedupe_key:
            prev = self.store.recent_with_key(
                dedupe_key, ts - self.cfg.dedupe_hours * 3600
            )
            if prev is not None:
                return NotifyResult("suppressed", prev.id, "duplicate of recent alert")

        def add(status: str) -> st.Alert:
            return self.store.add(
                created_at=ts,
                urgency=urgency,
                title=title,
                message=message,
                dedupe_key=dedupe_key,
                status=status,
            )

        if urgency == "low":
            return NotifyResult("digest", add(st.DIGEST).id, "saved for daily digest")

        critical = urgency == "critical"
        quiet = self.in_quiet_hours(now)
        if not critical:
            if urgency == "normal" and quiet:
                return NotifyResult("held", add(st.HELD).id, "quiet hours")
            if self._rate_limited(ts):
                return NotifyResult("held", add(st.HELD).id, "hourly limit reached")

        wants_call = critical or (urgency == "high" and not quiet)
        expects_ack = urgency in ("high", "critical") and self.cfg.can_receive_webhooks
        alert = add(st.SENT if expects_ack else st.DONE)

        body = f"[Jarvis] {title}" + (f"\n{message}" if message else "")
        if expects_ack:
            body += f"\nReply OK {alert.id} to acknowledge."
        texted = self._sms(body, ts, critical)
        if texted:
            self.store.update(alert.id, sms_sent=1)
        called = self._call(alert, ts) if wants_call else False

        if not texted and not called:
            self.store.update(alert.id, status=st.FAILED)
            return NotifyResult("failed", alert.id, "could not reach Twilio")
        return NotifyResult("called" if called else "texted", alert.id)

    def ack(self, alert_id: int) -> bool:
        alert = self.store.get(alert_id)
        if alert is None or alert.status == st.ACKED:
            return False
        self.store.update(alert_id, status=st.ACKED, acked_at=self._now().timestamp())
        return True

    def handle_sms_reply(self, from_number: str, body: str) -> Optional[str]:
        """Handle "OK 12" style replies from the owner. Returns a reply or None.

        Returns ``None`` when the message is not an acknowledgement, so the
        caller can pass it on to the normal chat agent.
        """
        if from_number != self.cfg.owner_phone:
            return None
        m = _ACK_RE.match(body or "")
        if not m:
            return None
        if m.group(2):
            ok = self.ack(int(m.group(2)))
            return (
                f"Alert {m.group(2)} acknowledged."
                if ok
                else (f"No open alert {m.group(2)}.")
            )
        open_alerts = self.store.by_status(st.SENT)
        if not open_alerts:
            return None  # plain "ok" with nothing open: let the agent see it
        for a in open_alerts:
            self.ack(a.id)
        return f"Acknowledged {len(open_alerts)} alert(s)."

    def handle_voice_digits(self, alert_id: int, digits: str) -> str:
        """Return TwiML for a keypress during a Jarvis call."""
        lang = self.cfg.voice_language
        alert = self.store.get(alert_id)
        if alert is None:
            return "<Response><Hangup/></Response>"
        if digits in ("1", "2"):
            self.ack(alert_id)
            said = "Acknowledged. Goodbye."
            if digits == "2":
                text = f"[Jarvis] {alert.title}" + (
                    f"\n{alert.message}" if alert.message else ""
                )
                self._sms(text, self._now().timestamp(), alert.urgency == "critical")
                said = "Acknowledged. I've sent you the details by text. Goodbye."
            return f'<Response><Say language="{lang}">{escape(said)}</Say></Response>'
        return (
            f'<Response><Say language="{lang}">Sorry, I did not get that. '
            "Goodbye.</Say></Response>"
        )

    def process_escalations(self) -> List[int]:
        """Re-call unacknowledged critical alerts. Returns alert ids re-called."""
        now = self._now().timestamp()
        wait = self.cfg.escalate_after_minutes * 60
        recalled: List[int] = []
        for a in self.store.by_status(st.SENT):
            if a.urgency != "critical":
                continue
            if a.calls >= self.cfg.max_call_attempts:
                self.store.update(a.id, status=st.DONE)
                self._sms(
                    f"[Jarvis] I couldn't reach you about: {a.title}. "
                    "Please check when you can.",
                    now,
                    True,
                )
                continue
            if now - a.last_call_at >= wait and self._call(a, now, retry=True):
                recalled.append(a.id)
        return recalled

    def flush_held(self) -> int:
        """Send held alerts as one text once quiet hours / rate limit allow."""
        held = self.store.by_status(st.HELD)
        if not held:
            return 0
        now = self._now()
        ts = now.timestamp()
        if self.in_quiet_hours(now) or self._rate_limited(ts):
            return 0
        if len(held) == 1:
            a = held[0]
            body = f"[Jarvis] {a.title}" + (f"\n{a.message}" if a.message else "")
        else:
            lines = [f"[Jarvis] {len(held)} updates while you were away:"]
            lines += [
                f"- {a.title}" + (f": {_clip(a.message, 160)}" if a.message else "")
                for a in held
            ]
            body = "\n".join(lines)
        if not self._sms(body, ts, False):
            return 0
        for a in held:
            self.store.update(a.id, status=st.DONE, sms_sent=1)
        return len(held)

    def send_digest_if_due(self, *, force: bool = False) -> int:
        """Send the daily digest of low-urgency items. Returns items sent."""
        now = self._now()
        today = now.date().isoformat()
        if not force:
            at = self.cfg.digest_at
            if at is None or now.time() < at or self.in_quiet_hours(now):
                return 0
            if self.store.get_meta("last_digest_date") == today:
                return 0
        items = self.store.by_status(st.DIGEST)
        self.store.set_meta("last_digest_date", today)
        if not items:
            return 0
        lines = [f"[Jarvis] Daily summary ({len(items)}):"]
        lines += [
            f"- {a.title}" + (f": {_clip(a.message, 140)}" if a.message else "")
            for a in items
        ]
        if not self._sms("\n".join(lines), now.timestamp(), False):
            return 0
        for a in items:
            self.store.update(a.id, status=st.DONE, sms_sent=1)
        return len(items)


def build_default_notifier(config: Optional[NotifyConfig] = None) -> Notifier:
    """Create a notifier from config.toml + credentials."""
    from openjarvis.notify.config import load_notify_config
    from openjarvis.notify.gateway import TwilioGateway

    cfg = config or load_notify_config()
    gw = TwilioGateway(cfg.account_sid, cfg.auth_token, cfg.from_phone)
    return Notifier(cfg, gw)
