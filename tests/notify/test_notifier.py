"""Tests for the personal phone notifier (texts, calls, routing, escalation)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from openjarvis.notify import store as st
from openjarvis.notify.config import (
    MonitorConfig,
    NotifyConfig,
    load_notify_config,
    parse_quiet_hours,
)
from openjarvis.notify.gateway import build_call_twiml
from openjarvis.notify.notifier import Notifier

OWNER = "+15550001111"


class FakeGateway:
    def __init__(self, fail: bool = False) -> None:
        self.sms: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def send_sms(self, to: str, body: str) -> str:
        if self.fail:
            raise RuntimeError("twilio down")
        self.sms.append((to, body))
        return "SM1"

    def place_call(self, to: str, twiml: str) -> str:
        if self.fail:
            raise RuntimeError("twilio down")
        self.calls.append((to, twiml))
        return "CA1"


class Clock:
    def __init__(self, hour: int = 12, minute: int = 0) -> None:
        self.now = datetime(2026, 11, 15, hour, minute, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def make(
    hour: int = 12, public_url: str = "https://jarvis.example.com", **cfg_kw
) -> tuple[Notifier, FakeGateway, Clock]:
    cfg = NotifyConfig(
        owner_phone=OWNER,
        public_url=public_url,
        account_sid="AC1",
        auth_token="tok",
        from_phone="+15559998888",
        **cfg_kw,
    )
    gw = FakeGateway()
    clock = Clock(hour)
    return Notifier(cfg, gw, st.AlertStore(":memory:"), clock), gw, clock


# -------------------------------------------------------------------- config


def test_config_problems_and_ready() -> None:
    assert NotifyConfig().problems()  # nothing configured
    n, _, _ = make()
    assert n.cfg.problems() == []


def test_parse_quiet_hours() -> None:
    assert parse_quiet_hours("") is None
    start, end = parse_quiet_hours("23:00-07:30")
    assert (start.hour, end.hour, end.minute) == (23, 7, 30)
    with pytest.raises(ValueError):
        parse_quiet_hours("late")


def test_load_config_reads_toml_and_env(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[notify]\nowner_phone = "+15550001111"\nmax_per_hour = 2\n'
        'auth_token = "should-be-ignored"\n'
        "[notify.monitors]\ndisk_free_percent = 5\n"
    )
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACenv")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tokenv")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559998888")
    cfg = load_notify_config(path)
    assert cfg.owner_phone == OWNER
    assert cfg.max_per_hour == 2
    assert cfg.monitors.disk_free_percent == 5
    assert cfg.auth_token == "tokenv"  # secrets never come from config.toml
    assert cfg.problems() == []


# -------------------------------------------------------------------- routing


def test_low_goes_to_digest() -> None:
    n, gw, _ = make()
    r = n.notify("Newsletter", "nothing urgent", urgency="low")
    assert r.action == "digest"
    assert gw.sms == [] and gw.calls == []


def test_normal_texts_only() -> None:
    n, gw, _ = make()
    r = n.notify("Package delivered", "At the front door.")
    assert r.action == "texted"
    assert gw.sms[0][0] == OWNER
    assert "Package delivered" in gw.sms[0][1]
    assert gw.calls == []
    assert n.store.get(r.alert_id).status == st.DONE


def test_high_texts_and_calls_with_keypress() -> None:
    n, gw, _ = make()
    r = n.notify("Meeting in 10 min", "Standup with team", urgency="high")
    assert r.action == "called"
    assert len(gw.sms) == 1 and len(gw.calls) == 1
    assert f"Reply OK {r.alert_id}" in gw.sms[0][1]
    twiml = gw.calls[0][1]
    assert "<Gather" in twiml
    assert f"alert_id={r.alert_id}" in twiml
    assert n.store.get(r.alert_id).status == st.SENT


def test_no_public_url_means_no_ack_tracking() -> None:
    n, gw, _ = make(public_url="")
    r = n.notify("Server down", urgency="critical")
    assert r.action == "called"
    assert "<Gather" not in gw.calls[0][1]
    assert "Reply OK" not in gw.sms[0][1]
    assert n.store.get(r.alert_id).status == st.DONE


def test_quiet_hours_hold_normal_and_skip_call_for_high() -> None:
    n, gw, clock = make(hour=2)  # 02:00, inside 23:00-07:00
    assert n.notify("FYI").action == "held"
    r = n.notify("Important", urgency="high")
    assert r.action == "texted"
    assert gw.calls == []
    # After quiet hours the held text is delivered.
    clock.advance(hours=6)
    assert n.flush_held() == 1
    assert "FYI" in gw.sms[-1][1]


def test_critical_ignores_quiet_hours() -> None:
    n, gw, _ = make(hour=3)
    assert n.notify("Smoke alarm", urgency="critical").action == "called"
    assert len(gw.calls) == 1


def test_rate_limit_holds_extra_alerts() -> None:
    n, gw, _ = make(max_per_hour=2)
    assert n.notify("a").action == "texted"
    assert n.notify("b").action == "texted"
    r = n.notify("c")
    assert r.action == "held" and "limit" in r.detail
    # critical still goes through
    assert n.notify("fire", urgency="critical").action == "called"


def test_dedupe_suppresses_repeats_within_window() -> None:
    n, gw, clock = make()
    assert n.notify("Disk full", dedupe_key="disk:C").action == "texted"
    assert n.notify("Disk full", dedupe_key="disk:C").action == "suppressed"
    clock.advance(hours=7)
    assert n.notify("Disk full", dedupe_key="disk:C").action == "texted"


def test_failure_marks_alert_failed() -> None:
    n, gw, _ = make()
    gw.fail = True
    r = n.notify("x", urgency="high")
    assert r.action == "failed"
    assert n.store.get(r.alert_id).status == st.FAILED


def test_invalid_urgency() -> None:
    n, _, _ = make()
    with pytest.raises(ValueError):
        n.notify("x", urgency="panic")


# -------------------------------------------------------------------- escalation


def test_critical_escalates_until_acked() -> None:
    n, gw, clock = make(escalate_after_minutes=10, max_call_attempts=3)
    r = n.notify("Card used abroad", urgency="critical")
    assert len(gw.calls) == 1
    clock.advance(minutes=5)
    assert n.process_escalations() == []
    clock.advance(minutes=6)
    assert n.process_escalations() == [r.alert_id]
    assert "calling again" in gw.calls[-1][1]
    assert n.ack(r.alert_id)
    clock.advance(minutes=30)
    assert n.process_escalations() == []
    assert len(gw.calls) == 2


def test_escalation_gives_up_after_max_attempts() -> None:
    n, gw, clock = make(escalate_after_minutes=1, max_call_attempts=2)
    r = n.notify("Fire", urgency="critical")
    clock.advance(minutes=2)
    n.process_escalations()
    clock.advance(minutes=2)
    n.process_escalations()
    assert len(gw.calls) == 2
    assert n.store.get(r.alert_id).status == st.DONE
    assert "couldn't reach you" in gw.sms[-1][1]


def test_high_does_not_escalate() -> None:
    n, gw, clock = make(escalate_after_minutes=1)
    n.notify("x", urgency="high")
    clock.advance(minutes=30)
    assert n.process_escalations() == []


# -------------------------------------------------------------------- replies


def test_sms_ack_by_id_and_bare_ok() -> None:
    n, _, _ = make()
    a = n.notify("one", urgency="critical").alert_id
    b = n.notify("two", urgency="high").alert_id
    assert n.handle_sms_reply(OWNER, f"ok {a}") == f"Alert {a} acknowledged."
    assert n.store.get(a).status == st.ACKED
    assert n.handle_sms_reply(OWNER, "OK") == "Acknowledged 1 alert(s)."
    assert n.store.get(b).status == st.ACKED
    # Nothing open: let the normal chat agent handle it.
    assert n.handle_sms_reply(OWNER, "ok") is None


def test_sms_reply_ignored_from_strangers_and_non_acks() -> None:
    n, _, _ = make()
    a = n.notify("x", urgency="critical").alert_id
    assert n.handle_sms_reply("+15551112222", f"ok {a}") is None
    assert n.handle_sms_reply(OWNER, "what's on my calendar?") is None
    assert n.store.get(a).status == st.SENT


def test_voice_digits() -> None:
    n, gw, _ = make()
    a = n.notify("Flight delayed", "Now 9pm", urgency="critical").alert_id
    xml = n.handle_voice_digits(a, "2")
    assert "Acknowledged" in xml
    assert n.store.get(a).status == st.ACKED
    assert "Now 9pm" in gw.sms[-1][1]
    assert "did not get that" in n.handle_voice_digits(a, "9")
    assert "Hangup" in n.handle_voice_digits(9999, "1")


# -------------------------------------------------------------------- digest


def test_digest_sent_once_per_day_after_time() -> None:
    n, gw, clock = make(hour=19, digest_time="20:00")
    n.notify("Newsletter A", urgency="low")
    n.notify("Newsletter B", urgency="low")
    assert n.send_digest_if_due() == 0  # too early
    clock.advance(hours=1, minutes=5)
    assert n.send_digest_if_due() == 2
    assert "Daily summary (2)" in gw.sms[-1][1]
    n.notify("Newsletter C", urgency="low")
    assert n.send_digest_if_due() == 0  # already sent today
    assert n.send_digest_if_due(force=True) == 1


# -------------------------------------------------------------------- twiml


def test_twiml_escapes_text() -> None:
    xml = build_call_twiml('Tom & Jerry <script> "x"', gather_url="https://a/b?x=1&y=2")
    assert "<script>" not in xml
    assert "Tom &amp; Jerry" in xml
    assert 'action="https://a/b?x=1&amp;y=2"' in xml


# -------------------------------------------------------------------- monitors


def _fake_psutil(*, free_pct=50, battery=None, mem=40, procs=("explorer.exe",)):
    part = SimpleNamespace(mountpoint="C:\\", fstype="NTFS", opts="rw,fixed")
    cd = SimpleNamespace(mountpoint="D:\\", fstype="", opts="cdrom")
    proc_objs = [
        SimpleNamespace(info={"name": p, "memory_percent": 10.0}) for p in procs
    ]
    return SimpleNamespace(
        disk_partitions=lambda all=False: [part, cd],
        disk_usage=lambda mp: SimpleNamespace(
            percent=100 - free_pct, free=5 * 1024**3, total=200 * 1024**3
        ),
        sensors_battery=lambda: battery,
        virtual_memory=lambda: SimpleNamespace(percent=mem),
        process_iter=lambda attrs=None: list(proc_objs),
    )


def test_monitors_all_ok() -> None:
    from openjarvis.notify import monitors

    assert monitors.run_all(MonitorConfig(), _fake_psutil()) == []


def test_monitors_find_problems() -> None:
    from openjarvis.notify import monitors

    batt = SimpleNamespace(percent=4, power_plugged=False)
    cfg = MonitorConfig(watch_processes=["OneDrive.exe", "explorer.exe"])
    found = monitors.run_all(cfg, _fake_psutil(free_pct=3, battery=batt, mem=97))
    by_key = {f.key: f for f in found}
    assert by_key["disk:C:\\"].urgency == "high"
    assert by_key["battery:low"].urgency == "high"
    assert "memory:high" in by_key
    assert "process:onedrive.exe" in by_key
    assert "process:explorer.exe" not in by_key


def test_disk_check_skips_readonly_and_removable() -> None:
    from openjarvis.notify import monitors

    ps = _fake_psutil(free_pct=1)
    ps.disk_partitions = lambda all=False: [
        SimpleNamespace(mountpoint="/snap/x", fstype="squashfs", opts="ro"),
        SimpleNamespace(mountpoint="/opt/ro", fstype="ext4", opts="ro,nosuid"),
        SimpleNamespace(mountpoint="E:\\", fstype="FAT32", opts="rw,removable"),
    ]
    assert monitors.check_disks(MonitorConfig(), ps) == []


def test_monitors_plugged_in_battery_ok() -> None:
    from openjarvis.notify import monitors

    batt = SimpleNamespace(percent=4, power_plugged=True)
    assert monitors.check_battery(MonitorConfig(), _fake_psutil(battery=batt)) == []


def test_monitor_error_does_not_stop_others() -> None:
    from openjarvis.notify import monitors

    ps = _fake_psutil(mem=99)
    ps.disk_partitions = lambda all=False: (_ for _ in ()).throw(OSError("boom"))
    found = monitors.run_all(MonitorConfig(), ps)
    keys = {f.key for f in found}
    assert "monitor-error:check_disks" in keys
    assert "memory:high" in keys


def test_watcher_tick_dedupes_findings() -> None:
    from openjarvis.notify import watcher

    n, gw, _ = make()
    ps = _fake_psutil(free_pct=3)
    watcher.tick(n, ps)
    watcher.tick(n, ps)
    disk_texts = [b for _, b in gw.sms if "Disk" in b]
    assert len(disk_texts) == 1
    assert len(gw.calls) == 1  # high urgency disk alert calls once


# -------------------------------------------------------------------- tool


def test_notify_tool() -> None:
    from openjarvis.tools.notify_tool import NotifyUserTool

    n, gw, _ = make()
    tool = NotifyUserTool(notifier=n)
    assert tool.spec.name == "notify_user"
    res = tool.execute(title="Bill due tomorrow", message="Rent", urgency="normal")
    assert res.success and res.content == "Texted the user."
    assert tool.execute(title="", urgency="normal").success is False
    assert tool.execute(title="x", urgency="panic").success is False


def test_notify_tool_not_configured() -> None:
    from openjarvis.tools.notify_tool import NotifyUserTool

    n = Notifier(NotifyConfig(), FakeGateway(), st.AlertStore(":memory:"))
    res = NotifyUserTool(notifier=n).execute(title="x", urgency="normal")
    assert not res.success
    assert "not set up" in res.content


class CallFailsGateway(FakeGateway):
    def place_call(self, to: str, twiml: str) -> str:
        raise RuntimeError("trial accounts have limited parameter access")


def test_failed_calls_fall_back_to_texts_and_stay_bounded() -> None:
    cfg = NotifyConfig(
        owner_phone=OWNER,
        public_url="https://jarvis.example.com",
        account_sid="AC1",
        auth_token="tok",
        from_phone="+15559998888",
        escalate_after_minutes=10,
        max_call_attempts=3,
    )
    gw = CallFailsGateway()
    clock = Clock()
    n = Notifier(cfg, gw, st.AlertStore(":memory:"), clock)
    r = n.notify("Fire", "Kitchen", urgency="critical")
    assert r.action == "texted"
    assert n.store.get(r.alert_id).calls == 1
    clock.advance(minutes=5)
    n.process_escalations()
    assert len(gw.sms) == 1  # waits between attempts even when calls fail
    clock.advance(minutes=6)
    n.process_escalations()
    assert "URGENT (call failed): Fire" in gw.sms[-1][1]
    clock.advance(minutes=11)
    n.process_escalations()
    clock.advance(minutes=11)
    n.process_escalations()  # max attempts reached -> give up with a final text
    assert n.store.get(r.alert_id).status == st.DONE
    assert "couldn't reach you" in gw.sms[-1][1]
    count = len(gw.sms)
    clock.advance(hours=2)
    n.process_escalations()
    assert len(gw.sms) == count
