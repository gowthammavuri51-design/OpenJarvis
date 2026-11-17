"""Texts via Telegram (calls stay on Twilio)."""

from __future__ import annotations

import httpx
import pytest

from openjarvis.notify import store as st
from openjarvis.notify.config import NotifyConfig
from openjarvis.notify.gateway import SplitGateway, TelegramError, TelegramTexter
from openjarvis.notify.notifier import Notifier
from tests.notify.test_notifier import OWNER, Clock, FakeGateway

TOKEN = "123456:SECRET-token"
API = f"https://api.telegram.org/bot{TOKEN}"


class FakeTexter:
    def __init__(self, chat_id: str = "42") -> None:
        self.chat_id = chat_id
        self.sent: list[str] = []
        self.updates: list[dict] = []
        self.offsets: list[int] = []

    def send_text(self, body: str) -> str:
        self.sent.append(body)
        return "1"

    def get_updates(self, offset: int = 0, timeout: int = 0) -> list:
        self.offsets.append(offset)
        return [u for u in self.updates if u["update_id"] >= offset]


def _cfg(**kw) -> NotifyConfig:
    base = dict(
        owner_phone=OWNER,
        account_sid="AC1",
        auth_token="tok",
        from_phone="+15559998888",
        telegram_token=TOKEN,
        telegram_chat_id="42",
    )
    base.update(kw)
    return NotifyConfig(**base)


def _notifier(**kw):
    texter = FakeTexter()
    caller = FakeGateway()
    n = Notifier(
        _cfg(**kw), SplitGateway(texter, caller), st.AlertStore(":memory:"), Clock()
    )
    return n, texter, caller


# ---------------------------------------------------------------- config


def test_auto_uses_telegram_when_configured() -> None:
    assert _cfg().texts_via == "telegram"
    assert _cfg(telegram_chat_id="").texts_via == "sms"
    assert _cfg(text_channel="sms").texts_via == "sms"
    assert _cfg().problems() == []


def test_forced_telegram_without_setup_is_a_problem() -> None:
    probs = _cfg(text_channel="telegram", telegram_token="").problems()
    assert any("TELEGRAM_BOT_TOKEN" in p for p in probs)
    assert _cfg(text_channel="pigeon").problems()


def test_telegram_enables_replies_without_public_url() -> None:
    assert _cfg().can_receive_replies
    assert not _cfg(telegram_chat_id="").can_receive_replies


def test_load_config_reads_telegram(tmp_path, monkeypatch) -> None:
    from openjarvis.notify.config import load_notify_config

    path = tmp_path / "config.toml"
    path.write_text('[notify]\nowner_phone = "+15550001111"\ntelegram_chat_id = 42\n')
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg = load_notify_config(path)
    assert cfg.telegram_chat_id == "42"
    assert cfg.texts_via == "telegram"


# ---------------------------------------------------------------- routing


def test_texts_go_to_telegram_calls_to_twilio() -> None:
    n, texter, caller = _notifier()
    r = n.notify("Server down", "web01", urgency="high")
    assert r.action == "called"
    assert "Server down" in texter.sent[0]
    assert f"Reply OK {r.alert_id}" in texter.sent[0]
    assert caller.sms == []
    assert len(caller.calls) == 1
    assert n.store.get(r.alert_id).status == st.SENT


def test_critical_call_explains_how_to_stop() -> None:
    n, _, caller = _notifier()
    n.notify("Fire", urgency="critical")
    assert "reply OK to my Telegram message" in caller.calls[0][1]


def test_telegram_ok_reply_stops_escalation() -> None:
    n, texter, caller = _notifier()
    a = n.notify("Fire", urgency="critical").alert_id
    texter.updates = [
        {"update_id": 5, "message": {"chat": {"id": 999}, "text": f"ok {a}"}},
        {"update_id": 6, "message": {"chat": {"id": 42}, "text": f"OK {a}"}},
    ]
    assert n.poll_telegram_replies() == 1
    assert n.store.get(a).status == st.ACKED
    assert texter.sent[-1] == f"Alert {a} acknowledged."
    # offset advances so the same messages are not processed again
    assert n.poll_telegram_replies() == 0
    assert texter.offsets[-1] == 7


def test_stranger_cannot_ack() -> None:
    n, texter, _ = _notifier()
    a = n.notify("Fire", urgency="critical").alert_id
    texter.updates = [
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": f"ok {a}"}}
    ]
    n.poll_telegram_replies()
    assert n.store.get(a).status == st.SENT


def test_poll_without_telegram_is_noop() -> None:
    n = Notifier(_cfg(), FakeGateway(), st.AlertStore(":memory:"), Clock())
    assert n.poll_telegram_replies() == 0


def test_build_default_notifier_picks_split_gateway(monkeypatch) -> None:
    from openjarvis.notify.notifier import build_default_notifier

    n = build_default_notifier(_cfg())
    assert isinstance(n.gw, SplitGateway)
    n2 = build_default_notifier(_cfg(telegram_chat_id=""))
    assert not isinstance(n2.gw, SplitGateway)


# ---------------------------------------------------------------- HTTP


def test_texter_sends_message(respx_mock) -> None:
    route = respx_mock.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
    )
    t = TelegramTexter(TOKEN, "42", client=httpx.Client())
    assert t.send_text("hi") == "9"
    sent = route.calls[0].request
    assert b'"chat_id":"42"' in sent.content.replace(b" ", b"")


def test_texter_error_never_leaks_token(respx_mock) -> None:
    respx_mock.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(
            401, json={"ok": False, "description": "Unauthorized"}
        )
    )
    t = TelegramTexter(TOKEN, "42", client=httpx.Client())
    with pytest.raises(TelegramError) as info:
        t.send_text("hi")
    assert "Unauthorized" in str(info.value)
    assert TOKEN not in str(info.value)


def test_texter_network_error_never_leaks_token(respx_mock) -> None:
    respx_mock.post(f"{API}/getMe").mock(side_effect=httpx.ConnectError(f"{API} down"))
    t = TelegramTexter(TOKEN, "", client=httpx.Client())
    with pytest.raises(TelegramError) as info:
        t.bot_username()
    assert TOKEN not in str(info.value)
    assert "<token>" in str(info.value)


# ---------------------------------------------------------------- CLI helper


def test_save_chat_id_keeps_existing_config(tmp_path, monkeypatch) -> None:
    from openjarvis.cli import notify_cmd

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '# my settings\n[tools]\nenabled = ["notify_user"]\n\n'
        '[notify]\nowner_phone = "+15550001111"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("openjarvis.core.paths.get_config_path", lambda: cfg)
    notify_cmd._save_chat_id("42")
    text = cfg.read_text(encoding="utf-8")
    assert "# my settings" in text
    assert 'owner_phone = "+15550001111"' in text
    assert 'telegram_chat_id = "42"' in text


def test_save_chat_id_handles_bom_and_missing_section(tmp_path, monkeypatch) -> None:
    from openjarvis.cli import notify_cmd

    cfg = tmp_path / "config.toml"
    cfg.write_bytes("﻿[tools]\nenabled = []\n".encode("utf-8"))
    monkeypatch.setattr("openjarvis.core.paths.get_config_path", lambda: cfg)
    notify_cmd._save_chat_id("42")
    import tomllib

    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["notify"]["telegram_chat_id"] == "42"
