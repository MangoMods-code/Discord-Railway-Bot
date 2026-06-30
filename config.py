# config.py — Centralized config from environment variables.
# Railway: set these in the Railway dashboard env vars.
# Local: falls back to config.json if it exists.

import os
import json

_file_config = {}
_config_path = os.path.join(os.path.dirname(__file__), "config.json")
if os.path.exists(_config_path):
    with open(_config_path, "r") as f:
        _file_config = json.load(f)


def _get(key, default=""):
    return os.environ.get(key, _file_config.get(key, default))


# Discord
BOT_TOKEN       = _get("BOT_TOKEN")
OWNER_ID        = _get("OWNER_ID")
GUILD_ID        = _get("GUILD_ID")          # optional — omit to sync globally

# Channel IDs (comma-separated to support multiple)
LOG_CHANNEL_IDS = _get("LOG_CHANNEL_IDS")   # e.g. "123456,789012"
ALERT_CHANNEL_ID = _get("ALERT_CHANNEL_ID") # separate channel for crash/error alerts

# Railway API
RAILWAY_TOKEN   = _get("RAILWAY_TOKEN")

# Polling
POLL_INTERVAL   = int(_get("POLL_INTERVAL", "30"))     # seconds between polls
LOG_TAIL_LINES  = int(_get("LOG_TAIL_LINES", "15"))    # log lines to fetch per deployment
ALERT_COOLDOWN  = int(_get("ALERT_COOLDOWN", "120"))   # seconds before re-alerting same deployment

# Display
EMBED_COLOR     = _get("EMBED_COLOR", "8B5CF6")        # default purple to match Railway
BOT_NAME        = _get("BOT_NAME", "Railway Monitor")
BOT_FOOTER      = _get("BOT_FOOTER", "Railway Monitor")


def get_log_channel_ids() -> list[int]:
    """Parse LOG_CHANNEL_IDS into a list of ints."""
    raw = LOG_CHANNEL_IDS or ""
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids
