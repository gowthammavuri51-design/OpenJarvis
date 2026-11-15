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
enabled = ["notify_user", "shell_exec", "file_read", "web_search"]
```

Install the Twilio library: `uv sync --extra channel-twilio`.

### 4. Test it

```bash
jarvis notify setup        # shows what is still missing
jarvis notify test         # sends a test text
jarvis notify test --call  # text + test phone call
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

## Let the assistant reach you

With `notify_user` in `[tools].enabled`, the chat assistant can contact you
itself, for example:

> "Check if my D: drive backup folder was updated today, and call me if not."

The tool takes `title`, `message`, `urgency` (`low`/`normal`/`high`/
`critical`) and an optional `dedupe_key`.
