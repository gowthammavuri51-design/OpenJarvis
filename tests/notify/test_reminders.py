"""Reminders by text, and the texts-only (calls off) mode."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openjarvis.notify import store as st
from openjarvis.notify.config import NotifyConfig
from openjarvis.notify.gateway import SplitGateway
from openjarvis.notify.notifier import Notifier
from openjarvis.notify.reminders import describe, parse_reminder_request, parse_when
from tests.notify.test_notifier import OWNER, Clock, FakeGateway
from tests.notify.test_telegram import TOKEN, FakeTexter

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)  # Sunday noon


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("phrase", "expected", "daily"),
    [
        ("in 30 min", "2026-11-15 12:30", False),
        ("in 2 hours", "2026-11-15 14:00", False),
        ("in 1 day", "2026-11-16 12:00", False),
        ("in 45 seconds", "2026-11-15 12:00", False),
        ("at 6pm", "2026-11-15 18:00", False),
        ("at 6 PM", "2026-11-15 18:00", False),
        ("at 18:30", "2026-11-15 18:30", False),
        ("at 9am", "2026-11-16 09:00", False),  # already passed -> tomorrow
        ("at 12am", "2026-11-16 00:00", False),
        ("at 12pm", "2026-11-16 12:00", False),  # exactly now -> tomorrow
        ("tomorrow", "2026-11-16 09:00", False),
        ("tomorrow at 7:15 am", "2026-11-16 07:15", False),
        ("every day at 8am", "2026-11-16 08:00", True),
        ("daily at 20:00", "2026-11-15 20:00", True),
    ],
)
def test_parse_when(phrase, expected, daily) -> None:
    w = parse_when(phrase, NOW)
    assert w.due.strftime("%Y-%m-%d %H:%M") == expected
    assert w.daily is daily


@pytest.mark.parametrize("bad", ["soon", "at 25:00", "at 13pm", "in 5 fortnights", ""])
def test_parse_when_rejects(bad) -> None:
    with pytest.raises(ValueError):
        parse_when(bad, NOW)


@pytest.mark.parametrize(
    ("msg", "what", "due"),
    [
        ("remind me at 6pm to call mom", "call mom", "18:00"),
        ("Remind me in 20 min to check the oven", "check the oven", "12:20"),
        ("remind me to pay rent tomorrow at 9am", "pay rent", "09:00"),
        ("remind me about the meeting at 15:30", "the meeting", "15:30"),
        ("remind me every day at 8am to take medicine", "take medicine", "08:00"),
        ("remind me to drink water in 1 hour.", "drink water", "13:00"),
    ],
)
def test_parse_reminder_request(msg, what, due) -> None:
    when, text = parse_reminder_request(msg, NOW)
    assert text == what
    assert when.due.strftime("%H:%M") == due


def test_parse_reminder_request_not_a_reminder() -> None:
    assert parse_reminder_request("what's the weather", NOW) is None


def test_parse_reminder_request_without_time_explains() -> None:
    with pytest.raises(ValueError, match="Tell me when"):
        parse_reminder_request("remind me to call mom", NOW)


def test_describe() -> None:
    assert describe(NOW.replace(hour=18), NOW) == "today 18:00"
    assert describe(parse_when("tomorrow", NOW).due, NOW) == "tomorrow 09:00"
    assert describe(NOW, NOW, daily=True) == "every day 12:00"


# ---------------------------------------------------------------- notifier


def _telegram_notifier(**kw):
    cfg = NotifyConfig(
        owner_phone=OWNER,
        account_sid="AC1",
        auth_token="tok",
        from_phone="+15559998888",
        telegram_token=TOKEN,
        telegram_chat_id="42",
        **kw,
    )
    texter, caller, clock = FakeTexter(), FakeGateway(), Clock()
    n = Notifier(cfg, SplitGateway(texter, caller), st.AlertStore(":memory:"), clock)
    return n, texter, caller, clock


def test_one_off_reminder_is_sent_once() -> None:
    n, texter, _, clock = _telegram_notifier()
    rid, desc = n.add_reminder("call mom", "in 30 min")
    assert desc == "today 12:30"
    assert n.send_due_reminders() == 0
    clock.advance(minutes=31)
    assert n.send_due_reminders() == 1
    assert texter.sent[-1] == "[Jarvis] Reminder: call mom"
    assert n.send_due_reminders() == 0
    assert n.store.get_reminder(rid).status == "done"


def test_daily_reminder_repeats() -> None:
    n, texter, _, clock = _telegram_notifier()
    n.add_reminder("take medicine", "every day at 8am")
    clock.advance(hours=20, minutes=1)  # next day 08:01
    assert n.send_due_reminders() == 1
    clock.advance(minutes=10)
    assert n.send_due_reminders() == 0
    clock.advance(days=1)
    assert n.send_due_reminders() == 1
    assert texter.sent.count("[Jarvis] Reminder: take medicine") == 2


def test_daily_reminder_skips_missed_days_without_flooding() -> None:
    n, texter, _, clock = _telegram_notifier()
    n.add_reminder("water plants", "every day at 8am")
    clock.advance(days=5)  # laptop was off for days
    assert n.send_due_reminders() == 1
    assert n.send_due_reminders() == 0


def test_failed_send_retries_later() -> None:
    n, _, _, clock = _telegram_notifier()
    n.add_reminder("x", "in 1 min")
    clock.advance(minutes=2)
    n.gw = SplitGateway(FailingTexter(), FakeGateway())
    assert n.send_due_reminders() == 0
    n.gw = SplitGateway(FakeTexter(), FakeGateway())
    assert n.send_due_reminders() == 1


class FailingTexter(FakeTexter):
    def send_text(self, body: str) -> str:
        raise RuntimeError("offline")


def test_list_and_cancel() -> None:
    n, *_ = _telegram_notifier()
    rid, _ = n.add_reminder("a", "at 6pm")
    n.add_reminder("b", "every day at 8am")
    items = n.list_reminders()
    assert items[0].startswith(f"#{rid} today 18:00: a")
    assert "every day 08:00: b" in items[1]
    assert n.cancel_reminder(rid)
    assert not n.cancel_reminder(rid)
    assert len(n.list_reminders()) == 1


# ---------------------------------------------------------------- telegram


def _say(n, texter, text: str, update_id: int) -> str:
    texter.updates.append(
        {"update_id": update_id, "message": {"chat": {"id": 42}, "text": text}}
    )
    n.poll_telegram_replies()
    return texter.sent[-1] if texter.sent else ""


def test_telegram_reminder_conversation() -> None:
    n, texter, _, clock = _telegram_notifier()
    reply = _say(n, texter, "remind me at 6pm to call mom", 1)
    assert reply == "OK, I'll remind you today 18:00: call mom (#1)"
    assert "#1 today 18:00: call mom" in _say(n, texter, "reminders", 2)
    assert _say(n, texter, "cancel 1", 3) == "Reminder #1 cancelled."
    assert _say(n, texter, "reminders", 4) == "You have no reminders."
    assert "Tell me when" in _say(n, texter, "remind me to stretch", 5)
    assert "I can set reminders" in _say(n, texter, "hello?", 6)


def test_start_and_plain_ok_get_no_reply() -> None:
    n, texter, _, _ = _telegram_notifier()
    _say(n, texter, "/start", 1)
    _say(n, texter, "ok", 2)
    assert texter.sent == []


def test_telegram_reminder_from_stranger_ignored() -> None:
    n, texter, _, _ = _telegram_notifier()
    texter.updates.append(
        {
            "update_id": 1,
            "message": {"chat": {"id": 7}, "text": "remind me in 1 min to x"},
        }
    )
    n.poll_telegram_replies()
    assert n.list_reminders() == []


# ---------------------------------------------------------------- calls off


def test_calls_disabled_never_calls_and_nudges_by_text() -> None:
    n, texter, caller, clock = _telegram_notifier(
        calls_enabled=False, escalate_after_minutes=10, max_call_attempts=3
    )
    a = n.notify("Fire", urgency="critical")
    assert a.action == "texted"
    n.notify("Server slow", urgency="high")
    assert caller.calls == []
    clock.advance(minutes=5)
    n.process_escalations()
    assert len(texter.sent) == 2
    clock.advance(minutes=6)
    n.process_escalations()
    assert "URGENT, still waiting: Fire" in texter.sent[-1]
    n.ack(a.alert_id)
    clock.advance(minutes=30)
    n.process_escalations()
    assert "still waiting" in texter.sent[-1] and len(texter.sent) == 3


def test_texts_only_needs_no_twilio() -> None:
    cfg = NotifyConfig(calls_enabled=False, telegram_token=TOKEN, telegram_chat_id="42")
    assert cfg.problems() == []
    assert NotifyConfig(calls_enabled=True, telegram_token=TOKEN).problems()


# ---------------------------------------------------------------- tool


def test_set_reminder_tool() -> None:
    from openjarvis.tools.notify_tool import SetReminderTool

    n, *_ = _telegram_notifier()
    tool = SetReminderTool(notifier=n)
    res = tool.execute(text="call mom", when="at 6pm")
    assert res.success and "today 18:00" in res.content
    bad = tool.execute(text="call mom", when="whenever")
    assert not bad.success
