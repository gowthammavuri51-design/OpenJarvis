"""Disk cleanup (scan, then delete only on request) and AI chat by text."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from openjarvis.notify import cleanup
from openjarvis.notify.cleanup import Category
from tests.notify.test_reminders import _say, _telegram_notifier

DAY = 86400


def _file(path, size: int, age_days: float = 0) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))
    return str(path)


def _du(_):
    return SimpleNamespace(free=10 * 1024**3, total=100 * 1024**3)


@pytest.fixture
def junk(tmp_path):
    temp = tmp_path / "Temp"
    cache = tmp_path / "BrowserCache"
    personal = tmp_path / "Downloads"
    old_tmp = _file(temp / "old.tmp", 3000, age_days=3)
    new_tmp = _file(temp / "installer-running.tmp", 5000, age_days=0)
    nested = _file(temp / "sub" / "dir" / "x.log", 1000, age_days=5)
    cached = _file(cache / "data_1", 7000)
    movie = _file(personal / "movie.mkv", 4000)
    cats = [
        Category("temp", "Temporary files", [str(temp)], min_age_hours=24),
        Category("browser", "Browser caches", [str(cache)]),
        Category("empty", "Nothing here", [str(tmp_path / "missing")]),
    ]
    return SimpleNamespace(
        tmp=tmp_path,
        cats=cats,
        personal=[str(personal)],
        old_tmp=old_tmp,
        new_tmp=new_tmp,
        nested=nested,
        cached=cached,
        movie=movie,
        temp=temp,
    )


def test_scan_reports_without_deleting(junk) -> None:
    r = cleanup.scan(junk.cats, junk.personal, big_file_min=1, disk_usage=_du)
    by = {c.key: c for c in r.categories}
    assert by["temp"].size == 4000  # the fresh temp file is not counted
    assert by["browser"].size == 7000
    assert "empty" not in by  # nothing to free -> not offered
    assert [b.path for b in r.big_files] == [junk.movie]
    text = cleanup.format_scan(r)
    assert "1. Temporary files" in text and "clean all" in text
    assert "won't delete" in text
    for f in (junk.old_tmp, junk.new_tmp, junk.cached, junk.movie):
        assert os.path.exists(f)


def test_clean_only_selected_and_keeps_recent_and_personal(junk) -> None:
    r = cleanup.scan(junk.cats, junk.personal, disk_usage=_du)
    res = cleanup.clean(cleanup.select(r, "1"))
    assert res.freed == 4000 and res.deleted == 2
    assert not os.path.exists(junk.old_tmp)
    assert not os.path.exists(junk.nested)
    assert not os.path.exists(os.path.dirname(junk.nested))  # empty dirs removed
    assert os.path.exists(junk.new_tmp)  # too recent
    assert os.path.exists(junk.temp)  # root kept
    assert os.path.exists(junk.cached)  # category 2 not chosen
    assert os.path.exists(junk.movie)  # personal files never touched


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_clean_never_follows_links_out_of_the_junk_folder(junk, tmp_path) -> None:
    precious = _file(tmp_path / "Documents" / "thesis.docx", 100, age_days=10)
    try:
        os.symlink(tmp_path / "Documents", junk.temp / "link-to-docs")
        os.symlink(
            precious,
            junk.temp / "link-to-file",
        )
    except OSError:
        pytest.skip("symlinks not permitted")
    r = cleanup.scan(junk.cats, [], disk_usage=_du)
    cleanup.clean(cleanup.select(r, "all"))
    assert os.path.exists(precious)
    assert os.path.exists(tmp_path / "Documents")


def test_select() -> None:
    r = cleanup.ScanResult(
        [Category("a", "A", size=1), Category("b", "B", size=1)], [], 0, "C:\\", 0, 0
    )
    assert [c.key for c in cleanup.select(r, "all")] == ["a", "b"]
    assert [c.key for c in cleanup.select(r, "2, 1 2")] == ["b", "a"]
    for bad in ("3", "x", ""):
        with pytest.raises(ValueError):
            cleanup.select(r, bad)


def test_scan_result_roundtrip(junk) -> None:
    r = cleanup.scan(junk.cats, junk.personal, big_file_min=1, disk_usage=_du)
    back = cleanup.ScanResult.from_json(r.to_json())
    assert back.reclaimable == r.reclaimable
    assert back.categories[0].roots == r.categories[0].roots


def test_human() -> None:
    assert cleanup.human(512) == "512 B"
    assert cleanup.human(3 * 1024**3) == "3.0 GB"


# ---------------------------------------------------------------- telegram


def test_clear_space_conversation(junk) -> None:
    n, texter, _, _ = _telegram_notifier()
    n.cleanup_scan = lambda: cleanup.scan(
        junk.cats, junk.personal, big_file_min=1, disk_usage=_du
    )
    assert "Send 'clear space' first" in _say(n, texter, "clean all", 1)
    reply = _say(n, texter, "can you clear some space on my laptop?", 2)
    assert "I can safely free" in reply
    assert os.path.exists(junk.old_tmp)  # nothing deleted yet
    assert "no item 9" in _say(n, texter, "clean 9", 3)
    done = _say(n, texter, "clean all", 4)
    assert done.startswith("Done. Freed")
    assert not os.path.exists(junk.old_tmp)
    assert not os.path.exists(junk.cached)
    assert os.path.exists(junk.movie)
    # A scan is used once; cleaning again needs a fresh scan.
    assert "Send 'clear space' first" in _say(n, texter, "clean all", 5)


def test_old_scan_is_rejected(junk) -> None:
    n, texter, _, clock = _telegram_notifier()
    n.cleanup_scan = lambda: cleanup.scan(
        junk.cats, junk.personal, disk_usage=_du, now=clock().timestamp()
    )
    _say(n, texter, "free up space", 1)
    clock.advance(hours=1)
    assert "old" in _say(n, texter, "clean all", 2)
    assert os.path.exists(junk.old_tmp)


@pytest.mark.parametrize(
    "msg",
    ["clear space", "Free up some storage please", "I'm low on disk", "cleanup"],
)
def test_space_phrases_trigger_scan(msg) -> None:
    n, texter, _, _ = _telegram_notifier()
    n.cleanup_scan = lambda: cleanup.ScanResult([], [], 0, "C:\\", 1, 2)
    assert "No junk files" in _say(n, texter, msg, 1)


# ---------------------------------------------------------------- chat


def test_chat_uses_ai_with_history() -> None:
    n, texter, _, _ = _telegram_notifier()
    prompts = []
    n.ai = lambda p: prompts.append(p) or "Hi Gowtham!"
    assert _say(n, texter, "whats your name?", 1) == "Hi Gowtham!"
    _say(n, texter, "and what time is it?", 2)
    assert "Current local time" in prompts[1]
    assert "User: whats your name?\nJarvis: Hi Gowtham!" in prompts[1]


def test_chat_failure_is_friendly() -> None:
    n, texter, _, _ = _telegram_notifier()

    def boom(_):
        raise RuntimeError("engine down")

    n.ai = boom
    assert "isn't available" in _say(n, texter, "hi there", 1)


def test_chat_disabled_returns_help() -> None:
    n, texter, _, _ = _telegram_notifier(chat_with_ai=False)
    assert "Things I can do" in _say(n, texter, "hi there", 1)


def test_chat_tools_are_restricted() -> None:
    n, _, _, _ = _telegram_notifier(chat_tools=["shell_exec", "set_reminder"])
    calls = []

    class FakeJarvis:
        def ask(self, prompt, **kw):
            calls.append(kw)
            return "ok"

    n._jarvis = FakeJarvis()
    n._default_ai("hi")
    assert calls[0]["tools"] == ["set_reminder"]  # shell_exec dropped
