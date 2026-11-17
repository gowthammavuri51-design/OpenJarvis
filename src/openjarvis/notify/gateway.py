"""Twilio gateway: send SMS and place voice calls."""

from __future__ import annotations

from typing import Any, Optional, Protocol
from xml.sax.saxutils import escape, quoteattr


class Gateway(Protocol):
    def send_sms(self, to: str, body: str) -> str: ...

    def place_call(self, to: str, twiml: str) -> str: ...


class TwilioGateway:
    """Thin wrapper over the Twilio REST client (``pip install twilio``)."""

    def __init__(self, account_sid: str, auth_token: str, from_phone: str) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = from_phone
        self._client: Any = None

    def _c(self) -> Any:
        if self._client is None:
            try:
                from twilio.rest import Client
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "The twilio package is not installed. Run: "
                    "uv sync --extra channel-twilio"
                ) from exc
            self._client = Client(self._sid, self._token)
        return self._client

    def send_sms(self, to: str, body: str) -> str:
        msg = self._c().messages.create(body=body, from_=self._from, to=to)
        return str(getattr(msg, "sid", ""))

    def place_call(self, to: str, twiml: str) -> str:
        call = self._c().calls.create(twiml=twiml, from_=self._from, to=to)
        return str(getattr(call, "sid", ""))


def build_call_twiml(
    spoken: str,
    *,
    language: str = "en-US",
    gather_url: Optional[str] = None,
) -> str:
    """Build the TwiML Jarvis speaks when it calls you.

    With ``gather_url`` (requires a public HTTPS URL) the caller can press 1
    to acknowledge or 2 to get the details by text; otherwise the message is
    simply read twice.
    """
    lang = quoteattr(language)
    say = f"<Say language={lang}>{escape(spoken)}</Say>"
    if gather_url:
        prompt = (
            f"<Say language={lang}>Press 1 to acknowledge. "
            "Press 2 to acknowledge and get the details by text.</Say>"
        )
        gather = (
            f'<Gather numDigits="1" timeout="8" method="POST" '
            f"action={quoteattr(gather_url)}>{say}{prompt}</Gather>"
        )
        # If no key is pressed, read once more then hang up (stays unacked).
        return f"<Response>{gather}{say}<Say language={lang}>Goodbye.</Say></Response>"
    pause = '<Pause length="1"/>'
    return f"<Response>{say}{pause}{say}</Response>"


_TELEGRAM_API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class TelegramTexter:
    """Send texts to (and read replies from) the owner's Telegram chat.

    The bot token is part of every Telegram API URL, so it is scrubbed from
    any error before it can reach logs or the terminal.
    """

    def __init__(self, token: str, chat_id: str, client: Any = None) -> None:
        self._token = token
        self.chat_id = str(chat_id).strip()
        self._client = client

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=15.0)
        return self._client

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    def _call(self, method: str, **payload: Any) -> Any:
        url = f"{_TELEGRAM_API}/bot{self._token}/{method}"
        try:
            resp = self._http().post(url, json=payload)
            data = resp.json()
        except Exception as exc:
            raise TelegramError(
                self._scrub(f"Telegram {method} failed: {exc}")
            ) from None
        if not data.get("ok"):
            desc = data.get("description") or f"HTTP {resp.status_code}"
            raise TelegramError(self._scrub(f"Telegram {method} failed: {desc}"))
        return data.get("result")

    def send_text(self, body: str) -> str:
        result = self._call("sendMessage", chat_id=self.chat_id, text=body)
        return str((result or {}).get("message_id", ""))

    def send_photo(self, png: bytes, caption: str = "") -> str:
        url = f"{_TELEGRAM_API}/bot{self._token}/sendPhoto"
        try:
            resp = self._http().post(
                url,
                data={"chat_id": self.chat_id, "caption": caption[:1000]},
                files={"photo": ("screen.png", png, "image/png")},
                timeout=60.0,
            )
            data = resp.json()
        except Exception as exc:
            raise TelegramError(
                self._scrub(f"Telegram sendPhoto failed: {exc}")
            ) from None
        if not data.get("ok"):
            desc = data.get("description") or f"HTTP {resp.status_code}"
            raise TelegramError(self._scrub(f"Telegram sendPhoto failed: {desc}"))
        return str((data.get("result") or {}).get("message_id", ""))

    def get_updates(self, offset: int = 0, timeout: int = 0) -> list:
        return self._call("getUpdates", offset=offset, timeout=timeout) or []

    def bot_username(self) -> str:
        return str((self._call("getMe") or {}).get("username", ""))


class SplitGateway:
    """Texts through one service (e.g. Telegram), calls through Twilio."""

    def __init__(self, texter: TelegramTexter, caller: Gateway) -> None:
        self.texter = texter
        self.caller = caller

    def send_sms(self, to: str, body: str) -> str:
        # ``to`` is ignored: the Telegram chat already identifies the owner.
        return self.texter.send_text(body)

    def place_call(self, to: str, twiml: str) -> str:
        return self.caller.place_call(to, twiml)
