# Railway Monitor Bot — Setup

## What It Does
- Polls all Railway projects every N seconds (default 30)
- Posts Discord embeds on deployment status changes (BUILDING → DEPLOYING → SUCCESS/FAILED/CRASHED)
- Pings you directly on CRASHED or FAILED deployments
- Tail logs with `/railway logs <project>` on demand
- Trigger redeploys with `/railway redeploy <project>` (owner only)
- `/railway status` — live overview of every project + latest deployment

## Setup (Local)

1. `pip install -r requirements.txt`
2. Copy `config.example.json` → `config.json` and fill in values
3. `python bot.py`

## Setup (Railway Deployment)

Set these env vars in your Railway service dashboard:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Discord bot token |
| `RAILWAY_TOKEN` | ✅ | Railway API token (Account Settings → Tokens) |
| `OWNER_ID` | ✅ | Your Discord user ID (right-click → Copy User ID) |
| `LOG_CHANNEL_IDS` | ✅ | Comma-separated Discord channel IDs for log embeds |
| `ALERT_CHANNEL_ID` | ⬜ | Channel for crash/error pings (falls back to first log channel) |
| `GUILD_ID` | ⬜ | Server ID for instant slash command sync (blank = global, ~1hr delay) |
| `POLL_INTERVAL` | ⬜ | Seconds between polls (default: 30) |
| `LOG_TAIL_LINES` | ⬜ | Log lines per embed (default: 15, max enforced: 40) |
| `ALERT_COOLDOWN` | ⬜ | Seconds before re-alerting same deployment (default: 120) |

## Getting Railway Token
Railway dashboard → top-right avatar → Account Settings → Tokens → New Token

## Discord Bot Permissions Needed
- `Send Messages`
- `Embed Links`
- `View Channel`

OAuth2 URL: Developers Portal → OAuth2 → URL Generator → scope: `bot` → permissions above
