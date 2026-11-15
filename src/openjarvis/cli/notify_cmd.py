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
def test(call: bool) -> None:
    """Send yourself a test text (and optionally a call)."""
    n = _notifier()
    gw = n.gw
    try:
        gw.send_sms(n.cfg.owner_phone, "[Jarvis] Test text: notifications work.")
        click.echo("Test text sent.")
        if call:
            from openjarvis.notify.gateway import build_call_twiml

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
        click.echo(f"Failed: {exc}", err=True)
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
