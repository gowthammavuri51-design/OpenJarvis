"""Android phone link through the free MacroDroid app.

Laptop -> phone: Jarvis calls the phone's MacroDroid *webhook* URL
(``https://trigger.macrodroid.com/<device-id>/jarvis?action=...``); one
MacroDroid macro performs the action (ring, alarm, ringer mode, timer).

Phone -> laptop: a MacroDroid macro sends events (missed call, battery,
notification) straight to the laptop over the home Wi-Fi, to a small
listener started by ``jarvis notify watch``. Requests must carry a secret
token; nothing goes through a third-party server.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

logger = logging.getLogger(__name__)

WEBHOOK_HOST = "trigger.macrodroid.com"
WEBHOOK_NAME = "jarvis"

_CLOCK = r"(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>am|pm)?"
_RE_RING = re.compile(r"^(?:find|ring|where(?:'s| is))\s+(?:my\s+)?phone\??$")
_RE_ALARM = re.compile(rf"^(?:set\s+)?(?:an\s+)?alarm\s+(?:for\s+|at\s+)?{_CLOCK}$")
_RE_RINGER = re.compile(
    r"^(?:(?:put\s+)?(?:my\s+)?phone\s+(?:on|to)\s+(?P<a>silent|vibrate|normal|ring)"
    r"|(?P<b>silent|vibrate|normal|ring)\s+mode(?:\s+on)?)$"
)
_RE_TIMER = re.compile(
    r"^(?:set\s+)?(?:a\s+)?timer\s+(?:for\s+)?(?P<n>\d+)\s*(?P<u>s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hours?)$"
)


def valid_webhook(url: str) -> bool:
    parts = urlsplit(url or "")
    return (
        parts.scheme == "https"
        and parts.hostname == WEBHOOK_HOST
        and bool(parts.path.strip("/"))
    )


def webhook_full_url(url: str) -> str:
    """The URL to call: as pasted (any identifier, any case).

    A bare ``.../<device-id>`` gets the default ``/jarvis`` identifier.
    """
    parts = urlsplit((url or "").strip())
    segments = [seg for seg in parts.path.split("/") if seg]
    base = f"{parts.scheme}://{parts.netloc}/" + "/".join(segments)
    return base if len(segments) >= 2 else f"{base}/{WEBHOOK_NAME}"


def _to_24h(h: str, m: Optional[str], ampm: Optional[str]) -> Tuple[int, int]:
    hour, minute = int(h), int(m or 0)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError("invalid hour")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid time")
    return hour, minute


def parse_phone_command(text: str) -> Optional[Tuple[Dict[str, str], str]]:
    """Map a message to (webhook params, confirmation text), or None."""
    t = " ".join((text or "").lower().replace("please", " ").split()).rstrip(".!")
    if _RE_RING.match(t):
        return {"action": "ring"}, "Ringing your phone."
    m = _RE_ALARM.match(t)
    if m:
        try:
            hour, minute = _to_24h(m.group("h"), m.group("m"), m.group("ampm"))
        except ValueError:
            return {"action": "error"}, "That isn't a valid time for an alarm."
        return (
            {"action": "alarm", "hour": str(hour), "minute": f"{minute:02d}"},
            f"Alarm set on your phone for {hour:02d}:{minute:02d}.",
        )
    m = _RE_RINGER.match(t)
    if m:
        mode = m.group("a") or m.group("b")
        mode = "normal" if mode == "ring" else mode
        return {"action": "ringer", "mode": mode}, f"Phone set to {mode} mode."
    m = _RE_TIMER.match(t)
    if m:
        unit = m.group("u")[0]
        seconds = int(m.group("n")) * {"s": 1, "m": 60, "h": 3600}[unit]
        return (
            {"action": "timer", "seconds": str(seconds)},
            f"Timer started on your phone ({m.group('n')} {m.group('u')}).",
        )
    return None


class PhoneLink:
    """Send commands to the phone through its MacroDroid webhook."""

    def __init__(self, webhook_url: str, client: Any = None) -> None:
        self._url = webhook_full_url(webhook_url)
        self._client = client

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=15.0)
        return self._client

    def send(self, params: Dict[str, str]) -> None:
        url = f"{self._url}?{urlencode(params)}"
        try:
            resp = self._http().get(url)
        except Exception as exc:
            # The device id in the URL is a secret: keep it out of messages.
            raise RuntimeError(
                f"Could not reach MacroDroid ({type(exc).__name__})"
            ) from None
        if resp.status_code >= 400:
            raise RuntimeError(f"MacroDroid answered HTTP {resp.status_code}")


# ---------------------------------------------------------------- events


def new_token() -> str:
    return secrets.token_urlsafe(18)


def local_ip() -> str:
    """Best guess at the laptop's address on the home network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET address: nothing is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


EVENT_TYPES = ("missed_call", "battery", "notification", "sms", "ping")


def clean_event(params: Dict[str, str]) -> Optional[Dict[str, str]]:
    etype = (params.get("type") or "").strip().lower()
    if etype not in EVENT_TYPES:
        return None
    keep = {"type": etype}
    for key in ("from", "level", "charging", "app", "title", "text"):
        if key in params:
            keep[key] = str(params[key])[:200]
    return keep


def make_server(
    port: int,
    token: str,
    on_event: Callable[[Dict[str, str]], None],
    host: str = "0.0.0.0",
) -> ThreadingHTTPServer:
    """HTTP listener for phone events: GET/POST /phone?token=...&type=..."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis"

        def log_message(self, *args: Any) -> None:  # keep tokens out of logs
            return

        def _params(self) -> Dict[str, str]:
            parts = urlsplit(self.path)
            q = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if self.command == "POST":
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype:
                    try:
                        body = json.loads(raw or "{}")
                        if isinstance(body, dict):
                            q.update({k: str(v) for k, v in body.items()})
                    except ValueError:
                        pass
                else:
                    q.update({k: v[-1] for k, v in parse_qs(raw).items()})
            return q

        def _handle(self) -> None:
            if urlsplit(self.path).path.rstrip("/") != "/phone":
                self._reply(404, "not found")
                return
            params = self._params()
            given = params.pop("token", "")
            if not token or not hmac.compare_digest(given.encode(), token.encode()):
                self._reply(403, "forbidden")
                return
            event = clean_event(params)
            if event is None:
                self._reply(400, "unknown event type")
                return
            try:
                on_event(event)
            except Exception:
                logger.exception("phone event handling failed")
            self._reply(200, "ok")

        def _reply(self, code: int, text: str) -> None:
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _handle
        do_POST = _handle

    return ThreadingHTTPServer((host, port), Handler)


def start_server_thread(
    port: int, token: str, on_event: Callable[[Dict[str, str]], None]
) -> Optional[ThreadingHTTPServer]:
    try:
        server = make_server(port, token, on_event)
    except OSError as exc:
        logger.warning("Phone listener could not start on port %s: %s", port, exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def describe_event(e: Dict[str, str]) -> str:
    t = e.get("type")
    if t == "missed_call":
        return f"Missed call from {e.get('from') or 'unknown'}"
    if t == "battery":
        charging = str(e.get("charging", "")).lower() in ("1", "true", "yes", "on")
        return f"Phone battery {e.get('level', '?')}%" + (
            " (charging)" if charging else ""
        )
    if t in ("notification", "sms"):
        who = e.get("app") or e.get("from") or "phone"
        title = e.get("title") or ""
        return f"{who}: {title}".strip(": ")
    return "Phone check-in"


def battery_low(e: Dict[str, str], threshold: float) -> bool:
    try:
        level = float(e.get("level", "100"))
    except ValueError:
        return False
    charging = str(e.get("charging", "")).lower() in ("1", "true", "yes", "on")
    return not charging and level <= threshold


__all__: List[str] = [
    "PhoneLink",
    "parse_phone_command",
    "valid_webhook",
    "make_server",
    "start_server_thread",
    "clean_event",
    "describe_event",
    "battery_low",
    "local_ip",
    "new_token",
]
