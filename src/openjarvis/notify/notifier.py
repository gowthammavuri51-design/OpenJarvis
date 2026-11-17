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
# Tools the chat AI may use from Telegram. Nothing that runs commands or
# touches files: a message (or a web page it reads) must never be able to
# make the laptop execute something.
_SAFE_CHAT_TOOLS = frozenset(
    {"set_reminder", "web_search", "get_weather", "calculator", "think"}
)
_SPACE_RE = re.compile(
    r"\b(clear|free|clean|make|get|need|low on)\b.*\b(space|storage|disk)\b"
    r"|^/?(cleanup|clean up|disk|storage)$"
)
_CLEAN_RE = re.compile(r"^/?clean\s+(all|everything|[\d ,]+)$")
_HELP = (
    "Things I can do by text:\n"
    "- remind me in 30 min to drink water\n"
    "- remind me at 6pm to call mom\n"
    "- remind me every day at 8am to take medicine\n"
    "- reminders (list) / cancel 3\n"
    "- clear space (find junk files to delete)\n"
    "- OK 12 (acknowledge alert 12)\n"
    "- or just ask me anything"
)

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
        # Seams for tests: answer free-form chat, scan/clean disk.
        self.ai: Optional[Callable[[str], str]] = None
        self.cleanup_scan: Optional[Callable[[], object]] = None
        self.cleanup_clean: Optional[Callable[[list], object]] = None
        self._history: List[tuple[str, str]] = []

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
        gather_url = self._gather_url(alert.id)
        if (
            alert.urgency == "critical"
            and gather_url is None
            and self.cfg.texts_via == "telegram"
        ):
            spoken += " To stop these calls, reply OK to my Telegram message."
        twiml = build_call_twiml(
            spoken,
            language=self.cfg.voice_language,
            gather_url=gather_url,
        )
        # Count the attempt even if it fails, so retries stay spaced out and
        # bounded by max_call_attempts.
        self.store.update(alert.id, calls=alert.calls + 1, last_call_at=ts)
        try:
            self.gw.place_call(self.cfg.owner_phone, twiml)
        except Exception as exc:
            logger.error("Notify call failed: %s", exc)
            return False
        self.store.log_contact(ts, "call", alert.urgency == "critical")
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

        wants_call = self.cfg.calls_enabled and (
            critical or (urgency == "high" and not quiet)
        )
        expects_ack = urgency in ("high", "critical") and self.cfg.can_receive_replies
        alert = add(st.SENT if expects_ack else st.DONE)

        body = f"[Jarvis] {title}" + (f"\n{message}" if message else "")
        if expects_ack:
            body += f"\nReply OK {alert.id} to acknowledge."
        texted = self._sms(body, ts, critical)
        if texted:
            self.store.update(alert.id, sms_sent=1)
        called = self._call(alert, ts) if wants_call else False
        if not wants_call and critical:
            # Start the nudge timer from the first text.
            self.store.update(alert.id, last_call_at=ts)

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
        return self.handle_ack_text(body)

    def handle_ack_text(self, body: str) -> Optional[str]:
        """Acknowledge alerts from an already-verified owner message."""
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

    # ---------------------------------------------------------------- reminders

    def add_reminder(self, text: str, when_phrase: str) -> tuple[int, str]:
        """Add a reminder from a phrase like 'at 6pm'. Returns (id, when)."""
        from openjarvis.notify.reminders import parse_when

        now = self._now()
        when = parse_when(when_phrase, now)
        return self._store_reminder(text, when, now)

    def _store_reminder(self, text: str, when, now: datetime) -> tuple[int, str]:
        from openjarvis.notify.reminders import describe

        text = _clip(text.strip(), 300)
        if not text:
            raise ValueError("What should I remind you about?")
        r = self.store.add_reminder(
            created_at=now.timestamp(),
            due_at=when.due.timestamp(),
            text=text,
            daily=when.daily,
        )
        return r.id, describe(when.due, now, when.daily)

    def list_reminders(self) -> List[str]:
        from openjarvis.notify.reminders import describe

        now = self._now()
        out = []
        for r in self.store.pending_reminders():
            due = datetime.fromtimestamp(r.due_at, tz=now.tzinfo)
            out.append(f"#{r.id} {describe(due, now, bool(r.daily))}: {r.text}")
        return out

    def cancel_reminder(self, rid: int) -> bool:
        r = self.store.get_reminder(rid)
        if r is None or r.status != "pending":
            return False
        self.store.update_reminder(rid, status="cancelled")
        return True

    def send_due_reminders(self) -> int:
        """Text every reminder that is due. Daily ones are rescheduled."""
        from datetime import timedelta

        now = self._now()
        ts = now.timestamp()
        sent = 0
        for r in self.store.pending_reminders(due_before=ts):
            if not self._sms(f"[Jarvis] Reminder: {r.text}", ts, False):
                continue  # try again next cycle
            sent += 1
            if r.daily:
                due = datetime.fromtimestamp(r.due_at, tz=now.tzinfo)
                while due.timestamp() <= ts:
                    due += timedelta(days=1)
                self.store.update_reminder(r.id, due_at=due.timestamp())
            else:
                self.store.update_reminder(r.id, status="done")
        return sent

    def handle_owner_message(self, text: str) -> Optional[str]:
        """Answer a message from the owner (Telegram). Returns the reply."""
        from openjarvis.notify.reminders import parse_reminder_request

        body = (text or "").strip()
        if not body:
            return None
        ack = self.handle_ack_text(body)
        if ack:
            return ack
        low = body.lower().rstrip(".!?")
        if low in ("/start", "ok", "okay", "thanks", "thank you"):
            return None
        if low in ("reminders", "/reminders", "list", "my reminders"):
            items = self.list_reminders()
            return (
                "\n".join(["Your reminders:", *items])
                if items
                else ("You have no reminders.")
            )
        m = re.match(r"^/?(?:cancel|delete|remove)\s+#?(\d+)$", low)
        if m:
            rid = int(m.group(1))
            return (
                f"Reminder #{rid} cancelled."
                if self.cancel_reminder(rid)
                else f"No reminder #{rid}."
            )
        try:
            parsed = parse_reminder_request(body, self._now())
        except ValueError as exc:
            return str(exc)
        if parsed:
            when, what = parsed
            rid, desc = self._store_reminder(what, when, self._now())
            return f"OK, I'll remind you {desc}: {what} (#{rid})"
        if _CLEAN_RE.match(low):
            return self.cleanup_command(low.split(None, 1)[1])
        if _SPACE_RE.search(low):
            return self.cleanup_scan_text()
        if low in ("help", "/help"):
            return _HELP
        if self.cfg.chat_with_ai:
            return self.chat(body)
        return _HELP

    # ---------------------------------------------------------------- cleanup

    def cleanup_scan_text(self) -> str:
        from openjarvis.notify import cleanup

        result = (self.cleanup_scan or cleanup.scan)()
        self.store.set_meta("cleanup_scan", result.to_json())
        return cleanup.format_scan(result)

    def cleanup_command(self, choice: str) -> str:
        from openjarvis.notify import cleanup

        raw = self.store.get_meta("cleanup_scan")
        if not raw:
            return "Send 'clear space' first so I can check what's safe to delete."
        result = cleanup.ScanResult.from_json(raw)
        if self._now().timestamp() - result.scanned_at > 30 * 60:
            return "That scan is old. Send 'clear space' again first."
        try:
            picked = cleanup.select(result, choice)
        except ValueError as exc:
            return str(exc)
        done = (self.cleanup_clean or cleanup.clean)(picked)
        self.store.set_meta("cleanup_scan", "")
        free_after = None
        try:
            import shutil

            free_after = shutil.disk_usage(result.drive).free
        except OSError:
            pass
        return cleanup.format_clean(done, free_after)

    # ---------------------------------------------------------------- AI chat

    def chat(self, text: str) -> str:
        """Answer a free-form message with the local AI."""
        history = "".join(f"User: {u}\nJarvis: {a}\n" for u, a in self._history[-4:])
        prompt = (
            "You are Jarvis, the user's personal assistant, replying by text "
            "message on their phone. Answer briefly in plain text (no markdown). "
            f"Current local time: {self._now().strftime('%A %d %B %Y, %H:%M')}.\n"
            + (f"Recent conversation:\n{history}" if history else "")
            + f"User: {text}\nJarvis:"
        )
        try:
            answer = (self.ai or self._default_ai)(prompt).strip()
        except Exception as exc:
            logger.warning("AI chat failed: %s", exc)
            return (
                "Sorry, my AI brain isn't available right now. "
                "Is Ollama running on the laptop?"
            )
        answer = answer or "(no answer)"
        self._history = (self._history + [(text, answer)])[-4:]
        return _clip(answer, 3500)

    def _default_ai(self, prompt: str) -> str:
        if getattr(self, "_jarvis", None) is None:
            from openjarvis.sdk import Jarvis

            self._jarvis = Jarvis()
        tools = [t for t in self.cfg.chat_tools if t in _SAFE_CHAT_TOOLS]
        if tools:
            return self._jarvis.ask(prompt, agent="orchestrator", tools=tools)
        return self._jarvis.ask(prompt)

    def poll_telegram_replies(self) -> int:
        """Read new Telegram messages and acknowledge "OK" replies.

        Only messages from the configured owner chat are considered. Returns
        the number of replies handled.
        """
        texter = getattr(self.gw, "texter", None)
        if texter is None:
            return 0
        offset = int(self.store.get_meta("telegram_offset") or 0)
        updates = texter.get_updates(offset=offset, timeout=0)
        handled = 0
        for upd in updates:
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != texter.chat_id:
                continue
            reply = self.handle_owner_message(str(msg.get("text") or ""))
            if reply:
                handled += 1
                try:
                    texter.send_text(reply)
                except Exception as exc:
                    logger.warning("Telegram reply failed: %s", exc)
        self.store.set_meta("telegram_offset", str(offset))
        return handled

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
            if now - a.last_call_at < wait:
                continue
            if not self.cfg.calls_enabled:
                # Calls are off: nudge again by text until acknowledged.
                self.store.update(a.id, calls=a.calls + 1, last_call_at=now)
                self._sms(
                    f"[Jarvis] URGENT, still waiting: {a.title}"
                    + (f"\n{a.message}" if a.message else "")
                    + f"\nReply OK {a.id} to acknowledge.",
                    now,
                    True,
                )
                continue
            if self._call(a, now, retry=True):
                recalled.append(a.id)
            else:
                # The phone call could not be placed: repeat the text instead,
                # so a critical alert is never silently dropped.
                self._sms(
                    f"[Jarvis] URGENT (call failed): {a.title}"
                    + (f"\n{a.message}" if a.message else "")
                    + f"\nReply OK {a.id} to acknowledge.",
                    now,
                    True,
                )
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
    from openjarvis.notify.gateway import SplitGateway, TelegramTexter, TwilioGateway

    cfg = config or load_notify_config()
    gw: Gateway = TwilioGateway(cfg.account_sid, cfg.auth_token, cfg.from_phone)
    if cfg.texts_via == "telegram" and cfg.telegram_ready:
        gw = SplitGateway(TelegramTexter(cfg.telegram_token, cfg.telegram_chat_id), gw)
    return Notifier(cfg, gw)
