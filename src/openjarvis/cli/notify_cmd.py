"""``jarvis notify`` — phone calls / texts to the owner, and the health watcher."""

from __future__ import annotations

import sys
from datetime import datetime

import click


def _notifier(require_ready: bool = True):  # noqa: ANN202
    from openjarvis.notify import build_default_notifier

    n = build_default_notifier()
    if require_ready:
        problems = n.cfg.problems()
        if problems:
            click.echo("Phone notifications are not set up yet:", err=True)
            for p in problems:
                click.echo(f"  - {p}", err=True)
            click.echo("Run `jarvis notify setup` for instructions.", err=True)
            sys.exit(1)
    return n


@click.group("notify")
def notify() -> None:
    """Have Jarvis text or call your phone."""


@notify.command("setup")
def setup() -> None:
    """Show setup status and what to configure."""
    from openjarvis.core.paths import get_config_dir, get_config_path

    n = _notifier(require_ready=False)
    cfg = n.cfg
    problems = cfg.problems()
    click.echo(f"Owner phone:   {cfg.owner_phone or '(not set)'}")
    click.echo(f"Twilio number: {cfg.from_phone or '(not set)'}")
    keys = "set" if cfg.account_sid and cfg.auth_token else "missing"
    url = cfg.public_url or "(none: press-1 / reply-OK disabled)"
    click.echo(f"Twilio keys:   {keys}")
    click.echo(f"Public URL:    {url}")
    click.echo(f"Quiet hours:   {cfg.quiet_hours or '(off)'}")
    if cfg.texts_via == "telegram":
        chat = cfg.telegram_chat_id or "(not linked)"
        click.echo(f"Texts via:     Telegram (chat {chat})")
    else:
        click.echo("Texts via:     SMS (run `jarvis notify telegram` to use Telegram)")
    calls = "on" if cfg.calls_enabled else "off (texts only)"
    click.echo(f"Phone calls:   {calls}")
    click.echo("")
    if not problems:
        click.echo("Ready. Try: jarvis notify test")
        return
    click.echo("To finish setup:\n")
    click.echo(f"1. Add to {get_config_path()}:\n")
    click.echo('   [notify]\n   owner_phone = "+15551234567"   # your phone\n')
    click.echo(f"2. Add your Twilio keys to {get_config_dir() / 'credentials.toml'}:\n")
    click.echo(
        '   [notify_user]\n   TWILIO_ACCOUNT_SID = "AC..."\n'
        '   TWILIO_AUTH_TOKEN = "..."\n   TWILIO_PHONE_NUMBER = "+1..."\n'
    )
    click.echo("   (or set them as environment variables)")
    click.echo("\nProblems found:")
    for p in problems:
        click.echo(f"  - {p}")


@notify.command("test")
@click.option("--call", is_flag=True, help="Also place a test phone call.")
@click.option("--call-only", is_flag=True, help="Only place a test call (no text).")
def test(call: bool, call_only: bool) -> None:
    """Send yourself a test text and/or call. Each is tried separately."""
    from openjarvis.notify.gateway import build_call_twiml

    n = _notifier()
    gw = n.gw
    failed = False
    if not call_only:
        try:
            gw.send_sms(n.cfg.owner_phone, "[Jarvis] Test text: notifications work.")
            click.echo(f"Test text sent via {n.cfg.texts_via}.")
        except Exception as exc:
            failed = True
            click.echo(f"Text failed: {exc}", err=True)
    if call or call_only:
        try:
            gw.place_call(
                n.cfg.owner_phone,
                build_call_twiml(
                    "Hello, this is Jarvis. This is a test call. "
                    "Your phone notifications are working.",
                    language=n.cfg.voice_language,
                ),
            )
            click.echo("Test call placed.")
        except Exception as exc:
            failed = True
            click.echo(f"Call failed: {exc}", err=True)
    if failed:
        sys.exit(1)


@notify.command("send")
@click.argument("title")
@click.option("-m", "--message", default="", help="Details.")
@click.option(
    "-u",
    "--urgency",
    default="normal",
    type=click.Choice(["low", "normal", "high", "critical"]),
)
def send(title: str, message: str, urgency: str) -> None:
    """Send an alert through the normal routing rules."""
    r = _notifier().notify(title, message, urgency=urgency)
    click.echo(f"{r.action} (alert {r.alert_id}) {r.detail}".strip())


@notify.command("remind")
@click.argument("text")
@click.option(
    "--when",
    "when",
    required=True,
    help="e.g. 'in 30 min', 'at 6pm', 'tomorrow at 9am', 'every day at 8am'.",
)
def remind(text: str, when: str) -> None:
    """Set a reminder that Jarvis texts you (needs `notify watch` running)."""
    try:
        rid, desc = _notifier(require_ready=False).add_reminder(text, when)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(f"Reminder #{rid} set for {desc}: {text}")


@notify.command("reminders")
def reminders() -> None:
    """List upcoming reminders."""
    items = _notifier(require_ready=False).list_reminders()
    click.echo("\n".join(items) if items else "No reminders.")


@notify.command("cancel")
@click.argument("reminder_id", type=int)
def cancel(reminder_id: int) -> None:
    """Cancel a reminder."""
    ok = _notifier(require_ready=False).cancel_reminder(reminder_id)
    click.echo("Cancelled." if ok else "No pending reminder with that id.")


@notify.command("cleanup")
@click.option("--clean", "choice", default="", help="'all' or numbers like '1,3'.")
def cleanup_cmd(choice: str) -> None:
    """Find junk files that are safe to delete; delete them only if asked."""
    n = _notifier(require_ready=False)
    click.echo("Scanning (this can take a minute)...")
    click.echo(n.cleanup_scan_text())
    if not choice:
        return
    if not click.confirm(f"Delete the junk in '{choice}' now?", default=False):
        click.echo("Nothing deleted.")
        return
    click.echo(n.cleanup_command(choice))


@notify.command("status")
@click.option("-n", "--limit", default=15)
def status(limit: int) -> None:
    """List recent alerts."""
    n = _notifier(require_ready=False)
    rows = n.store.recent(limit)
    if not rows:
        click.echo("No alerts yet.")
        return
    for a in rows:
        when = datetime.fromtimestamp(a.created_at).strftime("%m-%d %H:%M")
        extra = f" calls={a.calls}" if a.calls else ""
        click.echo(f"#{a.id:<4} {when}  {a.urgency:<8} {a.status:<7}{extra}  {a.title}")


@notify.command("ack")
@click.argument("alert_id", type=int)
def ack(alert_id: int) -> None:
    """Acknowledge an alert (stops repeat calls)."""
    ok = _notifier(require_ready=False).ack(alert_id)
    click.echo("Acknowledged." if ok else "No open alert with that id.")


@notify.command("digest")
def digest() -> None:
    """Send the low-urgency summary text now."""
    count = _notifier().send_digest_if_due(force=True)
    click.echo(f"Sent {count} item(s)." if count else "Nothing to send.")


@notify.command("check")
def check() -> None:
    """Run laptop health checks once and print findings (sends nothing)."""
    from openjarvis.notify import monitors
    from openjarvis.notify.config import load_notify_config

    found = monitors.run_all(load_notify_config().monitors)
    if not found:
        click.echo("All checks OK.")
    for f in found:
        click.echo(f"[{f.urgency}] {f.title}: {f.message}")


@notify.command("watch")
@click.option("--once", is_flag=True, help="Run a single cycle and exit.")
def watch(once: bool) -> None:
    """Keep watching laptop health and deliver alerts (runs until stopped)."""
    from openjarvis.notify import watcher

    n = _notifier()
    if once:
        r = watcher.tick(n)
        click.echo(
            f"alerts={len(r.findings)} recalled={len(r.recalled)} "
            f"flushed={r.flushed} digest={r.digested}"
        )
        return
    click.echo(
        f"Jarvis is watching (every {n.cfg.check_interval_seconds}s). "
        "Press Ctrl+C to stop."
    )
    try:
        watcher.run_forever(n)
    except KeyboardInterrupt:
        click.echo("Stopped.")


def _save_chat_id(chat_id: str) -> None:
    """Write [notify].telegram_chat_id into config.toml, keeping its layout."""
    import tomlkit

    from openjarvis.core.paths import get_config_path

    path = get_config_path()
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    doc = tomlkit.parse(text)
    if "notify" not in doc:
        doc["notify"] = tomlkit.table()
    doc["notify"]["telegram_chat_id"] = str(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


@notify.command("telegram")
@click.option("--wait", default=180, help="Seconds to wait for your first message.")
def telegram(wait: int) -> None:
    """Link a Telegram bot so Jarvis's texts arrive in Telegram."""
    import time

    from openjarvis.core.credentials import save_credential
    from openjarvis.notify.config import load_notify_config
    from openjarvis.notify.gateway import TelegramError, TelegramTexter

    cfg = load_notify_config()
    token = cfg.telegram_token
    if not token:
        click.echo(
            "1. In Telegram, open @BotFather, send /newbot and follow the steps.\n"
            "2. BotFather replies with a token like 123456:ABC-DEF...\n"
        )
        token = click.prompt(
            "Paste the bot token (it stays hidden)", hide_input=True
        ).strip()

    probe = TelegramTexter(token, "")
    try:
        username = probe.bot_username()
    except TelegramError as exc:
        click.echo(f"That token did not work: {exc}", err=True)
        sys.exit(1)
    if token != cfg.telegram_token:
        save_credential("notify_user", "TELEGRAM_BOT_TOKEN", token)
        click.echo("Bot token saved.")

    click.echo(
        f"\nNow open Telegram, search for @{username}, open it and press START "
        "(or send it any message)."
    )
    click.echo(f"Waiting up to {wait} seconds...")
    deadline = time.monotonic() + wait
    chat_id = ""
    last_update = 0
    while time.monotonic() < deadline and not chat_id:
        try:
            updates = probe.get_updates(offset=last_update, timeout=0)
        except TelegramError as exc:
            click.echo(f"Could not read messages: {exc}", err=True)
            sys.exit(1)
        for upd in updates:
            last_update = max(last_update, int(upd.get("update_id", 0)) + 1)
            chat = (upd.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and chat.get("id") is not None:
                chat_id = str(chat["id"])
        if not chat_id:
            time.sleep(2)
    if not chat_id:
        click.echo("No message arrived. Run `jarvis notify telegram` again.", err=True)
        sys.exit(1)

    _save_chat_id(chat_id)
    texter = TelegramTexter(token, chat_id)
    texter.send_text(
        "Jarvis is connected. Your alerts will arrive here. "
        "Reply OK to an alert to acknowledge it."
    )
    # Don't treat the START message as a reply later.
    from openjarvis.notify.store import AlertStore

    store = AlertStore()
    store.set_meta("telegram_offset", str(last_update))
    store.close()
    click.echo("Linked! Check Telegram for a message from Jarvis.")
