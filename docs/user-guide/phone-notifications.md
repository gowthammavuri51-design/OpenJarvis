# Phone Calls & Texts

Jarvis can **text** and **call** your personal phone when something needs your
attention, watch your laptop's health, and send you a daily summary of minor
things. It uses [Twilio](https://www.twilio.com/) for real SMS and voice calls.

## How Jarvis decides to reach you

| Urgency    | Normal hours                                   | Quiet hours (default 23:00–07:00) |
|------------|------------------------------------------------|-----------------------------------|
| `low`      | Saved for the daily summary text               | Same                              |
| `normal`   | Text message                                   | Held, sent when quiet hours end   |
| `high`     | Text **and** phone call                        | Text only                         |
| `critical` | Text **and** call, **calls again** until you acknowledge | Same — ignores quiet hours |

Safeguards:

- **Only your number.** Jarvis can only ever contact `[notify].owner_phone`;
  the AI cannot choose another number.
- **Rate limit.** At most `max_per_hour` non-critical texts/calls per hour;
  extras are bundled into one text later.
- **No repeats.** The same problem (e.g. "disk C: almost full") is reported at
  most once every `dedupe_hours`.
- **Acknowledge** a call by pressing **1** (or **2** to also get the details by
  text), or reply **`OK 12`** to a text (`OK` alone acknowledges everything
  open). Critical alerts stop re-calling once acknowledged, and give up after
  `max_call_attempts`.

## Setup (Windows, macOS or Linux)

### 1. Create a Twilio account

1. Sign up at twilio.com and buy a phone number with **SMS + Voice**
   (about $1–2/month; calls and texts cost a few cents each).
2. On a trial account, verify your personal phone number under
   *Phone Numbers → Verified Caller IDs*.
3. Copy your **Account SID**, **Auth Token** and Twilio **phone number**.

### 2. Save the Twilio keys

Add them to `credentials.toml` in your OpenJarvis folder
(`%USERPROFILE%\.openjarvis\credentials.toml` on Windows,
`~/.openjarvis/credentials.toml` on macOS/Linux):

```toml
[notify_user]
TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_AUTH_TOKEN = "your-auth-token"
TWILIO_PHONE_NUMBER = "+15551234567"
```

Environment variables with the same names also work.

### 3. Configure `config.toml`

```toml
[notify]
owner_phone = "+15557654321"     # YOUR phone, with country code
quiet_hours = "23:00-07:00"      # "" to disable
max_per_hour = 6
escalate_after_minutes = 10
max_call_attempts = 3
digest_time = "20:00"            # daily summary of low-urgency items
check_interval_seconds = 300
dedupe_hours = 6
# public_url = "https://your-tunnel.example.com"   # see step 6

[notify.monitors]
disk_free_percent = 10           # alert when a drive has less free space
battery_percent = 15             # alert when unplugged and below this
memory_percent = 95
watch_processes = ["OneDrive.exe"]   # alert if any of these stop running

[tools]
enabled = ["notify_user", "set_reminder", "shell_exec", "file_read", "web_search"]
```

Install the Twilio library: `uv sync --extra channel-twilio`.

### 3b. (Recommended outside the US) Texts via Telegram

Twilio **trial** accounts can only send Twilio's own SMS templates
(error 572006), and many countries (e.g. India) block SMS from foreign
numbers. Telegram avoids both, is free, and lets you reply **OK** to
acknowledge alerts without any public URL. Calls still go through Twilio.

```bash
jarvis notify telegram
```

It asks for a bot token (create a bot with **@BotFather** → `/newbot`),
then waits while you open your new bot in Telegram and press **Start**.
The token is saved to `credentials.toml` and your chat id to
`[notify].telegram_chat_id`. Set `text_channel = "sms"` under `[notify]`
to go back to SMS.

### 4. Test it

```bash
jarvis notify setup        # shows what is still missing
jarvis notify test         # sends a test text
jarvis notify test --call  # text + test phone call (each tried separately)
jarvis notify send "Hello" -m "From the CLI" -u high
jarvis notify status       # recent alerts
```

### 5. Keep Jarvis watching

```bash
jarvis notify check   # run laptop health checks once (sends nothing)
jarvis notify watch   # keep watching; texts/calls you when something is wrong
```

**Start automatically on Windows** (runs at log-on, hidden), in PowerShell:

```powershell
$jarvis = (Get-Command jarvis).Source
schtasks /Create /TN "Jarvis Notify" /SC ONLOGON /RL LIMITED /F `
  /TR "`"$jarvis`" notify watch"
```

Remove it with `schtasks /Delete /TN "Jarvis Notify" /F`.

### 6. (Optional) Press 1 / reply OK to acknowledge

To acknowledge alerts from your phone, Twilio must be able to reach Jarvis:

1. Run the server: `jarvis serve --port 8000`
2. Expose it: `jarvis tunnel --port 8000` (needs `cloudflared`) and copy the
   `https://…` URL.
3. Set `public_url` in `[notify]` to that URL.
4. In Twilio, set your number's **Messaging → A message comes in** webhook to
   `https://…/webhooks/twilio` (HTTP POST).

All webhooks verify Twilio's signature with your Auth Token and reject
anything else. Without `public_url`, alerts still work — Jarvis just can't
know you've seen them, so critical alerts are called only once.

## Texts only (no calls)

Until your Twilio account can place calls, turn calls off:

```toml
[notify]
calls_enabled = false
```

High alerts then arrive as texts, and critical alerts are re-sent every
`escalate_after_minutes` (up to `max_call_attempts` times) until you reply
**OK**. With Telegram texts and calls off, Twilio is not needed at all.

## Reminders

Message your Telegram bot (the watcher must be running):

| You send | Jarvis does |
|---|---|
| `remind me in 30 min to drink water` | one reminder in 30 minutes |
| `remind me at 6pm to call mom` | today at 18:00 (tomorrow if already past) |
| `remind me to pay rent tomorrow at 9am` | tomorrow 09:00 |
| `remind me every day at 8am to take medicine` | every day at 08:00 |
| `reminders` | lists upcoming reminders |
| `cancel 3` | cancels reminder #3 |

Or from the terminal:

```bash
jarvis notify remind "call mom" --when "at 6pm"
jarvis notify reminders
jarvis notify cancel 3
```

With `set_reminder` in `[tools].enabled`, you can also just ask the
assistant: *"remind me tomorrow at 9am to pay the electricity bill"*.

## Chat and free up disk space by text

Anything else you send the bot is answered by the local AI
(`chat_with_ai = true`). From Telegram the AI may only use the tools in
`chat_tools` that are on a safe list (reminders, web search, weather,
calculator) — never shell commands or file access.

Send **`clear space`** (or "free up some storage") and Jarvis scans for junk
that is always safe to delete — temp files older than a day, browser
caches, crash dumps, pip/npm/Windows Update download caches and the
Recycle Bin — and replies with sizes. **Nothing is deleted until you reply
`clean all` or `clean 1 3`.** Personal folders (Documents, Downloads,
Desktop, Pictures, Videos, Music) are never cleaned; their biggest files
are only listed so you can decide. Same from the terminal:

```bash
jarvis notify cleanup               # scan only
jarvis notify cleanup --clean all   # scan, confirm, delete
```

## Let the assistant reach you

With `notify_user` in `[tools].enabled`, the chat assistant can contact you
itself, for example:

> "Check if my D: drive backup folder was updated today, and call me if not."

The tool takes `title`, `message`, `urgency` (`low`/`normal`/`high`/
`critical`) and an optional `dedupe_key`.
