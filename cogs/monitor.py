# cogs/monitor.py — Core Railway polling loop.
# Rate-aware: skips redundant fetches, only pings on CRASHED/FAILED.

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

TERMINAL_STATUSES = {"SUCCESS", "FAILED", "CRASHED", "REMOVED", "SKIPPED"}

# Statuses that post a silent embed to log channel (no ping)
NOTIFY_SILENT = {"SUCCESS", "BUILDING", "DEPLOYING", "SLEEPING", "REMOVED"}

# Statuses that post an embed AND ping owner
NOTIFY_LOUD = {"FAILED", "CRASHED"}

# Only fetch logs when a deployment just hit a terminal state for the first time
LOG_FETCH_ON_STATUSES = {"FAILED", "CRASHED", "SUCCESS"}

METRICS_ELIGIBLE_STATUSES = {"SUCCESS", "DEPLOYING"}


class MonitorCog(commands.Cog):
    def __init__(self, bot: commands.Bot, railway: RailwayClient):
        self.bot = bot
        self.railway = railway

        self._deployment_status: dict[str, str] = {}
        self._logged_deployments: set[str] = set()      # dep IDs we've already fetched logs for
        self._last_log_tail: dict[str, str] = {}
        self._alert_timestamps: dict[str, float] = {}
        self._metrics_alert_timestamps: dict[str, float] = {}
        self._tick_count = 0

        self.poll_loop.change_interval(seconds=cfg.POLL_INTERVAL)
        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()

    # ── CHANNELS ──────────────────────────────────────────────────────────────

    def _log_channels(self) -> list[discord.TextChannel]:
        return [
            ch for cid in cfg.get_log_channel_ids()
            if (ch := self.bot.get_channel(cid))
        ]

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

    async def _send_alert(self, embed: discord.Embed):
        ch = self._alert_channel()
        if not ch:
            return
        owner = cfg.OWNER_ID
        mention = f"<@{owner}>" if owner else ""
        try:
            await ch.send(content=mention, embed=embed)
        except discord.HTTPException as e:
            logger.warning("Failed to send alert: %s", e)

    # ── POLL LOOP ─────────────────────────────────────────────────────────────

    @tasks.loop(seconds=120)
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
        check_metrics = self._tick_count % cfg.METRICS_CHECK_EVERY == 0

        projects = await self.railway.get_projects()
        for project in projects:
            for env_edge in project["environments"]["edges"]:
                env = env_edge["node"]
                await self._check_env(project, env, check_metrics)

    async def _check_env(self, project: dict, env: dict, check_metrics: bool):
        try:
            # Only pull the single latest deployment — cuts calls by 3x
            deployments = await self.railway.get_recent_deployments(
                project["id"], env["id"], limit=1
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

        status_changed = status != prev_status

        if status_changed:
            embed = deploy_event_embed(
                project["name"], service_name, env["name"], dep_id, status
            )
            if status in NOTIFY_LOUD:
                await self._send_alert(embed)
            elif status in NOTIFY_SILENT:
                await self._send_to_log(embed)

        # Only fetch logs once per deployment, only on meaningful terminal states
        if (
            status_changed
            and status in LOG_FETCH_ON_STATUSES
            and dep_id not in self._logged_deployments
        ):
            self._logged_deployments.add(dep_id)
            await self._check_logs(project, env, dep, service_name, status)

        # Metrics on slower cadence, only for running services
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

        log_entries = await self.railway.get_deployment_logs(dep_id)
        messages = [e.get("message", "") for e in log_entries if e.get("message")]

        # Build logs only as fallback on failure
        if not messages and status == "FAILED":
            build_entries = await self.railway.get_build_logs(dep_id)
            messages = [e.get("message", "") for e in build_entries if e.get("message")]

        if not messages:
            return

        tail = messages[-cfg.LOG_TAIL_LINES:]
        severity = classify_log_batch(tail)

        # Don't post a log embed for a successful clean deployment — no errors, no warnings
        if severity == "info" and status == "SUCCESS":
            return

        embed = log_alert_embed(
            project["name"], service_name, env["name"], dep_id, severity, tail
        )

        if severity == "error":
            await self._send_alert(embed)
        else:
            await self._send_to_log(embed)

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

        if cpu_pct < cfg.CPU_THRESHOLD_PCT and mem_pct < cfg.MEMORY_THRESHOLD_PCT:
            return

        now = datetime.now(timezone.utc).timestamp()
        if now - self._metrics_alert_timestamps.get(service_id, 0) < cfg.METRICS_ALERT_COOLDOWN:
            return
        self._metrics_alert_timestamps[service_id] = now

        embed = metrics_embed(
            project["name"], service_name, env["name"], cpu_pct, mem_bytes, mem_limit
        )
        await self._send_alert(embed)


async def setup(bot: commands.Bot):
    railway = bot.railway  # type: ignore[attr-defined]
    await bot.add_cog(MonitorCog(bot, railway))
