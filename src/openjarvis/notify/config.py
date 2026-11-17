"""Configuration for the personal notifier (``[notify]`` in config.toml).

Example::

    [notify]
    owner_phone = "+15551234567"          # your personal phone (E.164)
    public_url = "https://jarvis.example.trycloudflare.com"  # optional
    quiet_hours = "23:00-07:00"           # local time; empty disables
    max_per_hour = 6                      # non-critical texts/calls per hour
    escalate_after_minutes = 10           # re-call unacknowledged critical alerts
    max_call_attempts = 3
    digest_time = "20:00"                 # daily summary of low-urgency items
    check_interval_seconds = 300          # how often `jarvis notify watch` checks
    dedupe_hours = 6                      # suppress repeats of the same alert

    [notify.monitors]
    disk_free_percent = 10
    battery_percent = 15
    memory_percent = 95
    watch_processes = ["OneDrive.exe"]

Twilio credentials are secrets and are read from ``credentials.toml`` (tool
``notify_user``) or the environment: ``TWILIO_ACCOUNT_SID``,
``TWILIO_AUTH_TOKEN``, ``TWILIO_PHONE_NUMBER``.

Texts can go through a Telegram bot instead of SMS (free, and works where
Twilio trial SMS or international SMS is blocked): set
``TELEGRAM_BOT_TOKEN`` in the same place and ``telegram_chat_id`` under
``[notify]`` (``jarvis notify telegram`` finds it for you).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

TOOL_NAME = "notify_user"
CREDENTIAL_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> time:
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


def parse_quiet_hours(value: str) -> Optional[Tuple[time, time]]:
    value = (value or "").strip()
    if not value:
        return None
    start, sep, end = value.partition("-")
    if not sep:
        raise ValueError(f"quiet_hours must look like '23:00-07:00', got {value!r}")
    return parse_hhmm(start), parse_hhmm(end)


def is_valid_phone(number: str) -> bool:
    return bool(_E164.match(number or ""))


@dataclass
class MonitorConfig:
    disk_free_percent: float = 10.0
    battery_percent: float = 15.0
    memory_percent: float = 95.0
    watch_processes: List[str] = field(default_factory=list)


@dataclass
class NotifyConfig:
    owner_phone: str = ""
    public_url: str = ""
    quiet_hours: str = "23:00-07:00"
    max_per_hour: int = 6
    escalate_after_minutes: float = 10.0
    max_call_attempts: int = 3
    digest_time: str = "20:00"
    check_interval_seconds: int = 300
    dedupe_hours: float = 6.0
    voice_language: str = "en-US"
    # Where text messages go: "sms" (Twilio), "telegram", or "auto"
    # (Telegram when a bot token and chat id are configured, else SMS).
    text_channel: str = "auto"
    # Phone calls for high/critical alerts. With calls off, critical alerts
    # repeat as texts until acknowledged.
    calls_enabled: bool = True
    # Answer free-form Telegram messages with the local AI, and which
    # (safe) tools it may use there.
    chat_with_ai: bool = True
    chat_tools: List[str] = field(default_factory=lambda: ["set_reminder"])
    telegram_chat_id: str = ""
    monitors: MonitorConfig = field(default_factory=MonitorConfig)

    # Secrets — never written back to config.toml.
    account_sid: str = field(default="", repr=False)
    auth_token: str = field(default="", repr=False)
    from_phone: str = ""
    telegram_token: str = field(default="", repr=False)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_token and str(self.telegram_chat_id).strip())

    @property
    def texts_via(self) -> str:
        channel = (self.text_channel or "auto").strip().lower()
        if channel == "auto":
            return "telegram" if self.telegram_ready else "sms"
        return channel

    @property
    def can_receive_replies(self) -> bool:
        """Whether the owner can acknowledge alerts from their phone."""
        return self.can_receive_webhooks or self.texts_via == "telegram"

    @property
    def quiet_window(self) -> Optional[Tuple[time, time]]:
        return parse_quiet_hours(self.quiet_hours)

    @property
    def digest_at(self) -> Optional[time]:
        return parse_hhmm(self.digest_time) if self.digest_time.strip() else None

    @property
    def can_receive_webhooks(self) -> bool:
        return self.public_url.startswith("https://")

    def problems(self) -> List[str]:
        """Return human-readable setup problems (empty when ready)."""
        out: List[str] = []
        needs_twilio = self.calls_enabled or self.texts_via == "sms"
        if needs_twilio and not is_valid_phone(self.owner_phone):
            out.append(
                "[notify].owner_phone is missing or not in +countrycode format "
                "(e.g. +15551234567)"
            )
        if needs_twilio and (not self.account_sid or not self.auth_token):
            out.append("Twilio TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
        if needs_twilio and not is_valid_phone(self.from_phone):
            out.append("Twilio TWILIO_PHONE_NUMBER is missing or invalid")
        if self.texts_via not in ("sms", "telegram"):
            out.append("[notify].text_channel must be 'sms', 'telegram' or 'auto'")
        elif self.texts_via == "telegram":
            if not self.telegram_token:
                out.append("TELEGRAM_BOT_TOKEN not set (see `jarvis notify telegram`)")
            if not str(self.telegram_chat_id).strip():
                out.append(
                    "[notify].telegram_chat_id not set (run `jarvis notify telegram`)"
                )
        for name, fn in (
            ("quiet_hours", lambda: self.quiet_window),
            ("digest_time", lambda: self.digest_at),
        ):
            try:
                fn()
            except ValueError as exc:
                out.append(f"[notify].{name}: {exc}")
        return out


_SECRET_FIELDS = ("account_sid", "auth_token", "telegram_token")


def _apply(obj: Any, data: dict) -> None:
    for key, value in data.items():
        if key == "monitors" and isinstance(value, dict):
            _apply(obj.monitors, value)
        elif hasattr(obj, key) and key not in _SECRET_FIELDS:
            setattr(obj, key, value)


def load_notify_config(config_path: Optional[Path] = None) -> NotifyConfig:
    """Load ``[notify]`` from config.toml plus Twilio secrets."""
    from openjarvis.core.credentials import get_tool_credential
    from openjarvis.core.paths import get_config_path

    cfg = NotifyConfig()
    path = config_path or get_config_path()
    if path.exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        section = data.get("notify")
        if isinstance(section, dict):
            _apply(cfg, section)

    def cred(key: str) -> str:
        try:
            return (get_tool_credential(TOOL_NAME, key) or "").strip()
        except Exception:
            return ""

    cfg.account_sid = cred("TWILIO_ACCOUNT_SID")
    cfg.auth_token = cred("TWILIO_AUTH_TOKEN")
    cfg.from_phone = cred("TWILIO_PHONE_NUMBER")
    cfg.telegram_token = cred("TELEGRAM_BOT_TOKEN")
    cfg.telegram_chat_id = str(cfg.telegram_chat_id or "").strip()
    cfg.public_url = (cfg.public_url or "").rstrip("/")
    return cfg
