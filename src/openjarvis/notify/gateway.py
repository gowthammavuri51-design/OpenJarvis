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
