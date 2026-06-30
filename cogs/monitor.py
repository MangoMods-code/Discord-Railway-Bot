# cogs/monitor.py — Core Railway polling loop. Detects new deployments,
# status changes, log anomalies, and build failures. Posts alerts to Discord.

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

import config as cfg
from helpers import (
    classify_log_batch,
    deploy_event_embed,
    log_alert_embed,
    metrics_embed,
    SEVERITY_COLORS,
)
from railway_api import RailwayClient, RailwayAPIError

logger = logging.getLogger("monitor")

# Statuses that indicate the deployment is no longer actively running.
TERMINAL_STATUSES = {"SUCCESS", "FAILED", "CRASHED", "REMOVED", "SKIPPED"}

# Statuses we want to fire a Discord embed for when first seen.
NOTIFY_ON_STATUS = {
    "SUCCESS", "FAILED", "CRASHED", "BUILDING", "DEPLOYING",
    "SLEEPING", "REMOVED",
}


class MonitorCog(commands.Cog):
    def __init__(self, bot: commands.Bot, railway: RailwayClient):
        self.bot = bot
        self.railway = railway

        # deployment_id → last seen status
        self._deployment_status: dict[str, str] = {}
        # deployment_id → last log tail joined (dedup guard)
        self._last_log_tail: dict[str, str] = {}
        # deployment_id → epoch of last alert (cooldown)
        self._alert_timestamps: dict[str, float] = {}
        # set of deployment_ids we've already sent a log alert for at "info" level
        # (we suppress info log alerts after the first one to avoid spam)
        self._info_alerted: set[str] = set()

        self.poll_loop.change_interval(seconds=cfg.POLL_INTERVAL)
        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()

    # ── CHANNEL RESOLUTION ────────────────────────────────────────────────────

    def _log_channels(self) -> list[discord.TextChannel]:
        channels = []
        for cid in cfg.get_log_channel_ids():
            ch = self.bot.get_channel(cid)
            if ch:
                channels.append(ch)
        return channels

    def _alert_channel(self) -> Optional[discord.TextChannel]:
        raw = cfg.ALERT_CHANNEL_ID
        if raw and str(raw).isdigit():
            return self.bot.get_channel(int(raw))
        # Fall back to the first log channel
        chans = self._log_channels()
        return chans[0] if chans else None

    async def _send_to_log(self, embed: discord.Embed):
        for ch in self._log_channels():
            try:
                await ch.send(embed=embed)
            except discord.HTTPException as e:
                logger.warning("Failed to send to log channel %s: %s", ch.id, e)

    async def _send_alert(self, embed: discord.Embed, content: str = ""):
        ch = self._alert_channel()
        if not ch:
            return
        try:
            await ch.send(content=content, embed=embed)
        except discord.HTTPException as e:
            logger.warning("Failed to send alert: %s", e)

    # ── POLL LOOP ─────────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def poll_loop(self):
        try:
            await self._tick()
        except RailwayAPIError as e:
            logger.error("Railway API error during poll: %s", e)
        except Exception as e:
            logger.error("Unexpected error during poll: %s", e, exc_info=True)

    @poll_loop.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _tick(self):
        projects = await self.railway.get_projects()
        for project in projects:
            for env_edge in project["environments"]["edges"]:
                env = env_edge["node"]
                await self._check_env(project, env)

    async def _check_env(self, project: dict, env: dict):
        try:
            deployments = await self.railway.get_recent_deployments(
                project["id"], env["id"], limit=3
            )
        except RailwayAPIError as e:
            logger.warning(
                "Failed to fetch deployments for %s/%s: %s",
                project["name"], env["name"], e,
            )
            return

        for dep in deployments:
            await self._process_deployment(project, env, dep)

    async def _process_deployment(self, project: dict, env: dict, dep: dict):
        dep_id = dep["id"]
        status = dep["status"]
        service_name = (dep.get("service") or {}).get("name", "unknown")

        prev_status = self._deployment_status.get(dep_id)
        self._deployment_status[dep_id] = status

        # Fire status change embed
        if status != prev_status and status in NOTIFY_ON_STATUS:
            embed = deploy_event_embed(
                project["name"], service_name, env["name"], dep_id, status
            )
            await self._send_to_log(embed)

            # High-priority ping for crash/fail
            if status in ("CRASHED", "FAILED"):
                owner = cfg.OWNER_ID
                mention = f"<@{owner}>" if owner else ""
                await self._send_alert(embed, content=mention)

        # Fetch and evaluate logs for terminal or active deployments
        if status in TERMINAL_STATUSES or prev_status != status:
            await self._check_logs(project, env, dep, service_name, status)

    async def _check_logs(
        self,
        project: dict,
        env: dict,
        dep: dict,
        service_name: str,
        status: str,
    ):
        dep_id = dep["id"]

        # Cooldown check
        now = datetime.now(timezone.utc).timestamp()
        last_alert = self._alert_timestamps.get(dep_id, 0)
        if now - last_alert < cfg.ALERT_COOLDOWN:
            return

        # Fetch runtime logs
        log_entries = await self.railway.get_deployment_logs(dep_id)
        messages = [e.get("message", "") for e in log_entries if e.get("message")]

        # If empty runtime logs and it's a build failure, pull build logs
        if not messages and status == "FAILED":
            build_entries = await self.railway.get_build_logs(dep_id)
            messages = [e.get("message", "") for e in build_entries if e.get("message")]

        if not messages:
            return

        tail = messages[-cfg.LOG_TAIL_LINES:]
        tail_key = "\n".join(tail)

        if self._last_log_tail.get(dep_id) == tail_key:
            return
        self._last_log_tail[dep_id] = tail_key

        severity = classify_log_batch(tail)

        # Suppress info-level log embeds after the first to avoid channel spam.
        # Errors and warnings always post.
        if severity == "info":
            if dep_id in self._info_alerted:
                return
            if status == "SUCCESS":
                return
            self._info_alerted.add(dep_id)

        embed = log_alert_embed(
            project["name"], service_name, env["name"], dep_id, severity, tail
        )

        if severity == "error":
            owner = cfg.OWNER_ID
            mention = f"<@{owner}>" if owner else ""
            await self._send_alert(embed, content=mention)
        else:
            await self._send_to_log(embed)

        self._alert_timestamps[dep_id] = now


async def setup(bot: commands.Bot):
    railway = bot.railway  # type: ignore[attr-defined]
    await bot.add_cog(MonitorCog(bot, railway))
