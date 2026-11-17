"""Laptop control and Android phone link (MacroDroid)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.notify.laptop import LaptopControl
from openjarvis.notify.phone import (
    PhoneLink,
    battery_low,
    clean_event,
    make_server,
    parse_phone_command,
    valid_webhook,
)
from tests.notify.test_reminders import _say, _telegram_notifier

NOON = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
HOOK = "https://trigger.macrodroid.com/dev-123"


class Recorder:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.ok = ok

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.ok, "" if self.ok else "access denied"


def _laptop(**kw) -> tuple[LaptopControl, Recorder]:
    rec = Recorder()
    ctl = LaptopControl(runner=rec, platform="win32", now=lambda: NOON, **kw)
    return ctl, rec


# ---------------------------------------------------------------- laptop


@pytest.mark.parametrize(
    ("msg", "argv", "confirm"),
    [
        ("lock laptop", ["rundll32.exe", "user32.dll,LockWorkStation"], False),
        (
            "please lock the screen",
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            False,
        ),
        ("sleep", ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], True),
        ("shutdown", ["shutdown", "/s", "/t", "60"], True),
        ("shut down the laptop in 30 min", ["shutdown", "/s", "/t", "1800"], True),
        ("turn off my pc at 11pm", ["shutdown", "/s", "/t", "39600"], True),
        ("restart", ["shutdown", "/r", "/t", "60"], True),
        ("cancel shutdown", ["shutdown", "/a"], False),
        ("open Chrome", ["cmd", "/c", "start", "", "chrome"], False),
        ("open task manager", ["cmd", "/c", "start", "", "taskmgr"], False),
    ],
)
def test_laptop_commands(msg, argv, confirm) -> None:
    ctl, rec = _laptop()
    plan = ctl.parse(msg)
    assert plan is not None and plan.confirm is confirm
    out = plan.run()
    assert out.ok
    assert rec.calls == [argv]


def test_volume_uses_fixed_script() -> None:
    ctl, rec = _laptop()
    assert ctl.parse("volume up").run().text == "Volume up."
    assert rec.calls[0][:3] == ["powershell", "-NoProfile", "-Command"]
    assert "[char]175" in rec.calls[0][3]


def test_open_only_allowed_apps_and_no_injection() -> None:
    ctl, rec = _laptop()
    out = ctl.parse("open chrome & del /q C:\\*").run()
    assert not out.ok and "only open apps on your list" in out.text
    assert rec.calls == []


def test_custom_apps() -> None:
    ctl, rec = _laptop(apps={"Spotify": "spotify:"})
    ctl.parse("open spotify").run()
    assert rec.calls == [["cmd", "/c", "start", "", "spotify:"]]
    assert not ctl.parse("open chrome").run().ok


def test_bad_shutdown_time() -> None:
    ctl, rec = _laptop()
    out = ctl.parse("shutdown in soon").run()
    assert not out.ok and rec.calls == []


def test_unrelated_messages_are_not_commands() -> None:
    ctl, _ = _laptop()
    for msg in ("what's the weather", "lock in the plan", "remind me at 6pm to x"):
        assert ctl.parse(msg) is None


def test_failed_command_reports_error() -> None:
    rec = Recorder(ok=False)
    ctl = LaptopControl(runner=rec, platform="win32", now=lambda: NOON)
    out = ctl.parse("lock").run()
    assert not out.ok and "access denied" in out.text


def test_screenshot_and_status() -> None:
    ps = SimpleNamespace(
        sensors_battery=lambda: SimpleNamespace(percent=80, power_plugged=True),
        disk_usage=lambda d: SimpleNamespace(free=12.5 * 1024**3, percent=93),
        virtual_memory=lambda: SimpleNamespace(percent=40),
        cpu_percent=lambda interval: 12,
        boot_time=lambda: 0,
    )
    ctl, _ = _laptop(screenshot=lambda: b"PNG", psutil_mod=ps)
    shot = ctl.parse("screenshot").run()
    assert shot.image == b"PNG"
    status = ctl.parse("laptop status").run().text
    assert "Battery: 80% (charging)" in status
    assert "Disk C:\\: 12.5 GB free (7%)" in status


# ---------------------------------------------------------------- telegram flow


class PhotoTexter:
    def __init__(self, base) -> None:
        self.base = base
        self.photos: list[tuple[bytes, str]] = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def send_photo(self, png: bytes, caption: str = "") -> str:
        self.photos.append((png, caption))
        return "1"


def _notifier_with_laptop(**kw):
    n, texter, caller, clock = _telegram_notifier(**kw)
    rec = Recorder()
    n.laptop = LaptopControl(
        runner=rec, platform="win32", now=clock, screenshot=lambda: b"PNG"
    )
    return n, texter, rec, clock


def test_lock_runs_immediately() -> None:
    n, texter, rec, _ = _notifier_with_laptop()
    assert _say(n, texter, "lock laptop", 1) == "Locked."
    assert len(rec.calls) == 1


def test_shutdown_needs_yes() -> None:
    n, texter, rec, _ = _notifier_with_laptop()
    ask = _say(n, texter, "shutdown in 30 min", 1)
    assert "Are you sure" in ask and rec.calls == []
    done = _say(n, texter, "yes", 2)
    assert "shut down in 30 minutes" in done
    assert rec.calls == [["shutdown", "/s", "/t", "1800"]]
    # 'yes' with nothing pending goes to the AI, not to the laptop
    n.ai = lambda p: "Yes what?"
    assert _say(n, texter, "yes", 3) == "Yes what?"
    assert len(rec.calls) == 1


def test_shutdown_declined_or_expired() -> None:
    n, texter, rec, clock = _notifier_with_laptop()
    _say(n, texter, "restart", 1)
    assert _say(n, texter, "no", 2) == "OK, I won't do it."
    _say(n, texter, "restart", 3)
    clock.advance(minutes=3)
    assert "expired" in _say(n, texter, "yes", 4)
    assert rec.calls == []


def test_screenshot_is_sent_as_photo() -> None:
    n, texter, _, _ = _notifier_with_laptop()
    photo = PhotoTexter(texter)
    n.gw.texter = photo
    texter.updates.append(
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "screenshot"}}
    )
    n.poll_telegram_replies()
    assert photo.photos == [(b"PNG", "Here's your screen.")]


def test_laptop_control_can_be_disabled() -> None:
    n, texter, rec, _ = _notifier_with_laptop()
    n.cfg.laptop.enabled = False
    n.ai = lambda p: "chat"
    assert _say(n, texter, "lock laptop", 1) == "chat"
    assert rec.calls == []


# ---------------------------------------------------------------- phone commands


@pytest.mark.parametrize(
    ("msg", "params"),
    [
        ("find my phone", {"action": "ring"}),
        ("where is my phone?", {"action": "ring"}),
        ("set alarm 6:30am", {"action": "alarm", "hour": "6", "minute": "30"}),
        ("alarm for 7pm", {"action": "alarm", "hour": "19", "minute": "00"}),
        ("set an alarm at 21:15", {"action": "alarm", "hour": "21", "minute": "15"}),
        ("silent mode", {"action": "silent"}),
        ("put my phone on vibrate", {"action": "vibrate"}),
        ("ring mode", {"action": "normal"}),
        ("timer 10 min", {"action": "timer", "seconds": "600"}),
        ("set a timer for 30 seconds", {"action": "timer", "seconds": "30"}),
    ],
)
def test_parse_phone_command(msg, params) -> None:
    parsed = parse_phone_command(msg)
    assert parsed is not None and parsed[0] == params


def test_parse_phone_command_rejects() -> None:
    assert parse_phone_command("alarm 25:00")[0] == {"action": "error"}
    assert parse_phone_command("how is my phone") is None


def test_valid_webhook() -> None:
    assert valid_webhook(HOOK)
    assert not valid_webhook("http://trigger.macrodroid.com/x")
    assert not valid_webhook("https://evil.example.com/x")
    assert not valid_webhook("https://trigger.macrodroid.com/")


def test_phone_link_calls_webhook(respx_mock) -> None:
    route = respx_mock.get(url__startswith=f"{HOOK}/jarvis").mock(
        return_value=httpx.Response(200)
    )
    PhoneLink(HOOK, client=httpx.Client()).send({"action": "alarm", "hour": "6"})
    assert route.calls[0].request.url.params["action"] == "alarm"


def test_phone_link_error_hides_device_id(respx_mock) -> None:
    respx_mock.get(url__startswith=HOOK).mock(side_effect=httpx.ConnectError(HOOK))
    with pytest.raises(RuntimeError) as info:
        PhoneLink(HOOK, client=httpx.Client()).send({"action": "ring"})
    assert "dev-123" not in str(info.value)


def test_phone_command_via_telegram() -> None:
    n, texter, _, _ = _telegram_notifier()
    assert "isn't linked" in _say(n, texter, "find my phone", 1)
    sent = []
    n.phone_link = SimpleNamespace(send=lambda p: sent.append(p))
    assert _say(n, texter, "set alarm 6:30am", 2).startswith("Alarm set")
    assert sent == [{"action": "alarm", "hour": "6", "minute": "30"}]


# ---------------------------------------------------------------- phone events


def test_clean_event_and_battery() -> None:
    assert clean_event({"type": "hack"}) is None
    e = clean_event({"type": "battery", "level": "9", "charging": "false", "x": "y"})
    assert e == {"type": "battery", "level": "9", "charging": "false"}
    assert battery_low(e, 15)
    assert not battery_low({"level": "9", "charging": "true"}, 15)
    assert not battery_low({"level": "abc"}, 15)


def test_event_server_requires_token() -> None:
    import threading

    got = []
    server = make_server(0, "s3cret", got.append, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(trust_env=False) as c:
            assert c.get(f"{base}/phone?token=nope&type=ping").status_code == 403
            assert c.get(f"{base}/phone?type=ping").status_code == 403
            assert c.get(f"{base}/other?token=s3cret").status_code == 404
            assert c.get(f"{base}/phone?token=s3cret&type=bad").status_code == 400
            ok = c.get(f"{base}/phone?token=s3cret&type=missed_call&from=Mom")
            assert ok.status_code == 200
            c.post(
                f"{base}/phone",
                content=json.dumps({"token": "s3cret", "type": "battery", "level": 12}),
                headers={"Content-Type": "application/json"},
            )
            c.post(
                f"{base}/phone",
                data={"token": "s3cret", "type": "ping"},
            )
    finally:
        server.shutdown()
    assert got == [
        {"type": "missed_call", "from": "Mom"},
        {"type": "battery", "level": "12"},
        {"type": "ping"},
    ]


def test_missed_call_creates_call_back_reminder() -> None:
    n, texter, _, clock = _telegram_notifier()
    n.handle_phone_event({"type": "missed_call", "from": "Mom"})
    items = n.list_reminders()
    assert len(items) == 1 and "Call back Mom (missed call at 12:00)" in items[0]
    clock.advance(minutes=31)
    n.send_due_reminders()
    assert "Call back Mom" in texter.sent[-1]
    assert "Missed call from Mom" in n.phone_activity_text()


def test_low_phone_battery_alerts_once() -> None:
    n, texter, _, _ = _telegram_notifier()
    n.handle_phone_event({"type": "battery", "level": "10", "charging": "false"})
    n.handle_phone_event({"type": "battery", "level": "9", "charging": "false"})
    alerts = [t for t in texter.sent if "Phone battery low" in t]
    assert len(alerts) == 1
    n.handle_phone_event({"type": "battery", "level": "50", "charging": "false"})
    assert len([t for t in texter.sent if "Phone battery low" in t]) == 1


def test_phone_activity_empty() -> None:
    n, texter, _, _ = _telegram_notifier()
    assert "No phone activity yet" in _say(n, texter, "phone activity", 1)


# ---------------------------------------------------------------- config


def test_config_nested_sections(tmp_path, monkeypatch) -> None:
    from openjarvis.notify.config import load_notify_config

    path = tmp_path / "config.toml"
    path.write_text(
        '[notify.laptop.apps]\nspotify = "spotify:"\n'
        '[notify.phone]\nlisten_port = 0\nwebhook_url = "ignored"\n'
    )
    monkeypatch.setenv("MACRODROID_WEBHOOK_URL", HOOK)
    cfg = load_notify_config(path)
    assert cfg.laptop.apps["spotify"] == "spotify:"
    assert cfg.laptop.apps["chrome"] == "chrome"  # defaults kept
    assert cfg.phone.listen_port == 0
    assert cfg.phone.webhook_url == HOOK  # secret only from credentials/env


def test_powershell_screenshot_fallback() -> None:
    import re

    from openjarvis.notify.laptop import _grab_screen_powershell

    seen = []

    def runner(argv):
        seen.append(argv)
        path = re.search(r"\$bmp\.Save\('([^']+)'", argv[-1]).group(1)
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG fake")
        return True, ""

    assert _grab_screen_powershell(runner) == b"\x89PNG fake"
    assert seen[0][:3] == ["powershell", "-NoProfile", "-NonInteractive"]
    assert "CopyFromScreen" in seen[0][-1]


def test_powershell_screenshot_failure_cleans_up() -> None:
    from openjarvis.notify.laptop import _grab_screen_powershell

    with pytest.raises(RuntimeError, match="blocked"):
        _grab_screen_powershell(lambda argv: (False, "blocked"))


def test_webhook_url_keeps_pasted_identifier(respx_mock) -> None:
    from openjarvis.notify.phone import webhook_full_url

    assert webhook_full_url(HOOK) == f"{HOOK}/jarvis"
    assert webhook_full_url(f"{HOOK}/Jarvis/") == f"{HOOK}/Jarvis"
    route = respx_mock.get(url__startswith=f"{HOOK}/Jarvis?").mock(
        return_value=httpx.Response(200)
    )
    PhoneLink(f"{HOOK}/Jarvis", client=httpx.Client()).send({"action": "ring"})
    assert route.called
