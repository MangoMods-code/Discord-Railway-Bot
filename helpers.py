# helpers.py — Shared embed builders and utilities for Railway Monitor.

import re
from datetime import datetime, timezone

import discord
import config as cfg

EMBED_COLOR = discord.Colour(int(cfg.EMBED_COLOR, 16))

# ── SEVERITY CLASSIFICATION ───────────────────────────────────────────────────

_ERROR_PATTERNS = [
    re.compile(r"\b(error|exception|traceback|fatal|panic|critical|fail(ed|ure)?)\b", re.I),
    re.compile(r"\b(segfault|segmentation fault|killed|oom|out of memory)\b", re.I),
    re.compile(r"exit(ed)? (with )?code [^0]", re.I),
    re.compile(r"(unhandled|uncaught) (exception|rejection|error)", re.I),
    re.compile(r"(TypeError|ValueError|KeyError|AttributeError|RuntimeError|ImportError)", re.I),
    re.compile(r"(ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EACCES|ENOENT)", re.I),
]
_WARN_PATTERNS = [
    re.compile(r"\b(warn(ing)?|deprecated|deprecation)\b", re.I),
    re.compile(r"\b(timeout|timed out|slow query|high latency)\b", re.I),
    re.compile(r"\b(retry(ing)?|reconnect(ing)?|backoff)\b", re.I),
]

SEVERITY_COLORS = {
    "error":  discord.Color.from_str("#FF4444"),
    "warn":   discord.Color.from_str("#FFA500"),
    "info":   discord.Color.from_str("#8B5CF6"),
    "deploy": discord.Color.from_str("#22C55E"),
    "crash":  discord.Color.from_str("#FF0000"),
    "build_fail": discord.Color.from_str("#EF4444"),
}

STATUS_EMOJI = {
    "SUCCESS":   "✅",
    "FAILED":    "❌",
    "CRASHED":   "💥",
    "BUILDING":  "🔨",
    "DEPLOYING": "🚀",
    "SLEEPING":  "💤",
    "REMOVED":   "🗑️",
    "INITIALIZING": "⏳",
    "SKIPPED":   "⏭️",
    "QUEUED":    "🕐",
}

STATUS_COLORS = {
    "SUCCESS":   discord.Color.green(),
    "FAILED":    discord.Color.red(),
    "CRASHED":   discord.Color.from_str("#FF0000"),
    "BUILDING":  discord.Color.yellow(),
    "DEPLOYING": discord.Color.blurple(),
    "SLEEPING":  discord.Color.light_grey(),
}


def classify_line(line: str) -> str:
    for pat in _ERROR_PATTERNS:
        if pat.search(line):
            return "error"
    for pat in _WARN_PATTERNS:
        if pat.search(line):
            return "warn"
    return "info"


def classify_log_batch(lines: list[str]) -> str:
    severities = {classify_line(l) for l in lines}
    if "error" in severities:
        return "error"
    if "warn" in severities:
        return "warn"
    return "info"


def truncate(text: str, max_len: int = 1000) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:
        return f"{b / 1024**2:.1f} MB"
    return f"{b / 1024**3:.2f} GB"


def railway_footer(embed: discord.Embed, deployment_id: str = "") -> discord.Embed:
    suffix = f" • dep:{deployment_id[:8]}" if deployment_id else ""
    embed.set_footer(text=f"🚂 {cfg.BOT_FOOTER}{suffix}")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ── EMBED BUILDERS ────────────────────────────────────────────────────────────

def deploy_event_embed(
    project_name: str,
    service_name: str,
    env_name: str,
    deployment_id: str,
    status: str,
) -> discord.Embed:
    emoji = STATUS_EMOJI.get(status, "❓")
    color = STATUS_COLORS.get(status, EMBED_COLOR)
    embed = discord.Embed(title=f"{emoji}  {project_name} — {status}", color=color)
    embed.add_field(name="Service", value=service_name, inline=True)
    embed.add_field(name="Environment", value=env_name, inline=True)
    embed.add_field(name="Deployment", value=f"`{deployment_id[:12]}…`", inline=True)
    return railway_footer(embed, deployment_id)


def log_alert_embed(
    project_name: str,
    service_name: str,
    env_name: str,
    deployment_id: str,
    severity: str,
    tail_lines: list[str],
) -> discord.Embed:
    severity_label = {"error": "🔴 Error", "warn": "🟠 Warning", "info": "🔵 Log"}.get(
        severity, "📋 Log"
    )
    color = SEVERITY_COLORS.get(severity, EMBED_COLOR)
    embed = discord.Embed(
        title=f"{severity_label} — {project_name}",
        color=color,
    )
    embed.add_field(name="Service", value=service_name, inline=True)
    embed.add_field(name="Environment", value=env_name, inline=True)
    embed.add_field(name="Lines shown", value=str(len(tail_lines)), inline=True)

    log_text = "\n".join(tail_lines)
    embed.add_field(
        name="Output",
        value=f"```\n{truncate(log_text, 990)}\n```",
        inline=False,
    )
    return railway_footer(embed, deployment_id)


def metrics_embed(
    project_name: str,
    service_name: str,
    env_name: str,
    cpu_pct: float,
    mem_bytes: int,
    mem_limit_bytes: int,
) -> discord.Embed:
    mem_pct = (mem_bytes / mem_limit_bytes * 100) if mem_limit_bytes else 0
    color = discord.Color.red() if cpu_pct > 85 or mem_pct > 85 else EMBED_COLOR
    embed = discord.Embed(title=f"📊  Resource Usage — {project_name}", color=color)
    embed.add_field(name="Service", value=service_name, inline=True)
    embed.add_field(name="Environment", value=env_name, inline=True)
    embed.add_field(name="CPU", value=f"**{cpu_pct:.1f}%**", inline=True)
    embed.add_field(
        name="Memory",
        value=f"**{format_bytes(mem_bytes)}** / {format_bytes(mem_limit_bytes)} ({mem_pct:.1f}%)",
        inline=True,
    )
    return railway_footer(embed)


def status_embed(title: str, description: str, color: discord.Color = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color or EMBED_COLOR)
    return railway_footer(embed)


def error_embed(message: str) -> discord.Embed:
    embed = discord.Embed(title="❌  Error", description=message, color=discord.Color.red())
    return railway_footer(embed)


def success_embed(message: str) -> discord.Embed:
    embed = discord.Embed(title="✅  Done", description=message, color=discord.Color.green())
    return railway_footer(embed)
