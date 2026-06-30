# cogs/monitor.py — Core Railway polling loop. Detects new deployments,
# status changes, log anomalies, build failures, and resource threshold breaches.

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

# Statuses where a running service actually has live metrics to pull.
METRICS_ELIGIBLE_STATUSES = {"SUCCESS", "DEPLOYING", "BUILDING"}


class MonitorCog(commands.Cog):
    def __init__(self, bot: commands.Bot, railway: RailwayClient):
        self.bot = bot
        self.railway = railway

        # deployment_id → last seen status
        self._deployment_status: dict[str, str] = {}
        # deployment_id → last log tail joined (dedup guard)
        self._last_log_tail: dict[str, str] = {}
        # deployment_id → epoch of last log alert (cooldown)
        self._alert_timestamps: dict[str, float] = {}
        # set of deployment_ids we've already sent a log alert for at "info" level
        self._info_alerted: set[str] = set()
        # service_id → epoch of last metrics alert (separate cooldown, longer window)
        self._metrics_alert_timestamps: dict[str, float] = {}

        self._tick_count = 0

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
        self._tick_count += 1
        check_metrics_this_tick = self._tick_count % cfg.METRICS_CHECK_EVERY == 0

        projects = await self.railway.get_projects()
        for project in projects:
            for env_edge in project["environments"]["edges"]:
                env = env_edge["node"]
                await self._check_env(project, env, check_metrics_this_tick)

    async def _check_env(self, project: dict, env: dict, check_metrics: bool):
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
            await self._process_deployment(project, env, dep, check_metrics)

    async def _process_deployment(
        self, project: dict, env: dict, dep: dict, check_metrics: bool
    ):
        dep_id = dep["id"]
        status = dep["status"]
        service = dep.get("service") or {}
        service_id = service.get("id")
        service_name = service.get("name", "unknown")

        prev_status = self._deployment_status.get(dep_id)
        self._deployment_status[dep_id] = status

        # Fire status change embed
        if status != prev_status and status in NOTIFY_ON_STATUS:
            embed = deploy_event_embed(
                project["name"], service_name, env["name"], dep_id, status
            )
            await self._send_to_log(embed)

            if status in ("CRASHED", "FAILED"):
                owner = cfg.OWNER_ID
                mention = f"<@{owner}>" if owner else ""
                await self._send_alert(embed, content=mention)

        # Fetch and evaluate logs for terminal or active deployments
        if status in TERMINAL_STATUSES or prev_status != status:
            await self._check_logs(project, env, dep, service_name, status)

        # Resource metrics — only for live services, only on the slower cadence
        if check_metrics and service_id and status in METRICS_ELIGIBLE_STATUSES:
            await self._check_metrics(project, env, service_id, service_name)

    async def _check_logs(
        self,
        project: dict,
        env: dict,
        dep: dict,
        service_name: str,
        status: str,
    ):
        dep_id = dep["id"]

        now = datetime.now(timezone.utc).timestamp()
        last_alert = self._alert_timestamps.get(dep_id, 0)
        if now - last_alert < cfg.ALERT_COOLDOWN:
            return

        log_entries = await self.railway.get_deployment_logs(dep_id)
        messages = [e.get("message", "") for e in log_entries if e.get("message")]

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

    async def _check_metrics(
        self, project: dict, env: dict, service_id: str, service_name: str
    ):
        metrics = await self.railway.get_service_metrics(service_id, env["id"])
        if not metrics:
            return

        cpu_pct = metrics.get("cpuPercentage") or 0.0
        mem_bytes = metrics.get("memoryUsageBytes") or 0
        mem_limit = metrics.get("memoryLimitBytes") or 0
        mem_pct = (mem_bytes / mem_limit * 100) if mem_limit else 0.0

        breached = cpu_pct >= cfg.CPU_THRESHOLD_PCT or mem_pct >= cfg.MEMORY_THRESHOLD_PCT
        if not breached:
            return

        now = datetime.now(timezone.utc).timestamp()
        last_alert = self._metrics_alert_timestamps.get(service_id, 0)
        if now - last_alert < cfg.METRICS_ALERT_COOLDOWN:
            return
        self._metrics_alert_timestamps[service_id] = now

        embed = metrics_embed(
            project["name"], service_name, env["name"], cpu_pct, mem_bytes, mem_limit
        )
        owner = cfg.OWNER_ID
        mention = f"<@{owner}>" if owner else ""
        await self._send_alert(embed, content=mention)


async def setup(bot: commands.Bot):
    railway = bot.railway  # type: ignore[attr-defined]
    await bot.add_cog(MonitorCog(bot, railway))
