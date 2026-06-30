# cogs/commands.py — Slash commands for manual Railway interaction.
# /railway status   — live overview of all projects
# /railway logs     — tail logs for a specific deployment
# /railway metrics  — on-demand CPU/memory check
# /railway redeploy — trigger a redeploy
# /railway projects — list all projects with service counts

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config as cfg
from helpers import (
    error_embed, success_embed, status_embed,
    deploy_event_embed, log_alert_embed, metrics_embed, EMBED_COLOR,
    truncate, format_bytes, railway_footer, classify_log_batch,
    STATUS_EMOJI, STATUS_COLORS,
)
from railway_api import RailwayClient, RailwayAPIError

logger = logging.getLogger("commands")


def is_owner(interaction: discord.Interaction) -> bool:
    return str(interaction.user.id) == str(cfg.OWNER_ID)


class RailwayCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, railway: RailwayClient):
        self.bot = bot
        self.railway = railway

    railway_group = app_commands.Group(
        name="railway",
        description="Monitor and manage Railway deployments",
    )

    # ── /railway status ───────────────────────────────────────────────────────

    @railway_group.command(name="status", description="Live overview of all Railway projects and services")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            projects = await self.railway.get_projects()
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"Railway API error:\n```{e}```"))

        if not projects:
            return await interaction.followup.send(
                embed=status_embed("Railway Status", "No projects found on this token.")
            )

        pages = []
        for project in projects:
            envs = [e["node"] for e in project["environments"]["edges"]]
            services = [e["node"] for e in project["services"]["edges"]]

            embed = discord.Embed(
                title=f"🚂  {project['name']}",
                color=EMBED_COLOR,
            )
            embed.add_field(
                name="Services",
                value="\n".join(f"• {s['name']}" for s in services) or "None",
                inline=True,
            )
            embed.add_field(
                name="Environments",
                value="\n".join(f"• {e['name']}" for e in envs) or "None",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            dep_lines = []
            for env in envs[:3]:
                try:
                    deps = await self.railway.get_recent_deployments(
                        project["id"], env["id"], limit=1
                    )
                    if deps:
                        d = deps[0]
                        svc = (d.get("service") or {}).get("name", "?")
                        emoji = STATUS_EMOJI.get(d["status"], "❓")
                        dep_lines.append(
                            f"{emoji} **{env['name']}** / {svc} — `{d['status']}`"
                        )
                    else:
                        dep_lines.append(f"⬜ **{env['name']}** — no deployments")
                except RailwayAPIError:
                    dep_lines.append(f"⚠️ **{env['name']}** — fetch failed")

            if dep_lines:
                embed.add_field(
                    name="Latest Deployments",
                    value="\n".join(dep_lines),
                    inline=False,
                )

            railway_footer(embed)
            pages.append(embed)

        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0])
        else:
            view = PaginatorView(pages, interaction.user.id)
            await interaction.followup.send(embed=pages[0], view=view)

    # ── /railway projects ─────────────────────────────────────────────────────

    @railway_group.command(name="projects", description="List all Railway projects")
    async def projects(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            projects = await self.railway.get_projects()
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"```{e}```"))

        if not projects:
            return await interaction.followup.send(
                embed=status_embed("Projects", "No projects found.")
            )

        lines = []
        for p in projects:
            svc_count = len(p["services"]["edges"])
            env_count = len(p["environments"]["edges"])
            lines.append(
                f"**{p['name']}** — {svc_count} service(s), {env_count} environment(s)\n"
                f"> `{p['id']}`"
            )

        embed = discord.Embed(
            title=f"🚂  Railway Projects ({len(projects)})",
            description="\n\n".join(lines),
            color=EMBED_COLOR,
        )
        railway_footer(embed)
        await interaction.followup.send(embed=embed)

    # ── /railway logs ─────────────────────────────────────────────────────────

    @railway_group.command(
        name="logs",
        description="Tail logs for the latest deployment in a project",
    )
    @app_commands.describe(
        project_name="Name of the Railway project (case-insensitive)",
        environment="Environment name (default: production)",
        lines="Number of log lines to show (default: 15, max: 40)",
    )
    async def logs(
        self,
        interaction: discord.Interaction,
        project_name: str,
        environment: str = "production",
        lines: int = 15,
    ):
        await interaction.response.defer(ephemeral=True)
        lines = max(1, min(lines, 40))

        try:
            projects = await self.railway.get_projects()
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"```{e}```"))

        project = next(
            (p for p in projects if p["name"].lower() == project_name.lower()), None
        )
        if not project:
            names = ", ".join(f"`{p['name']}`" for p in projects)
            return await interaction.followup.send(
                embed=error_embed(
                    f"Project `{project_name}` not found.\n\nAvailable: {names}"
                )
            )

        env = next(
            (
                e["node"]
                for e in project["environments"]["edges"]
                if e["node"]["name"].lower() == environment.lower()
            ),
            None,
        )
        if not env:
            env_names = ", ".join(
                f"`{e['node']['name']}`" for e in project["environments"]["edges"]
            )
            return await interaction.followup.send(
                embed=error_embed(
                    f"Environment `{environment}` not found.\n\nAvailable: {env_names}"
                )
            )

        try:
            deps = await self.railway.get_recent_deployments(
                project["id"], env["id"], limit=1
            )
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"```{e}```"))

        if not deps:
            return await interaction.followup.send(
                embed=error_embed(
                    f"No deployments found for **{project_name}** / **{environment}**."
                )
            )

        dep = deps[0]
        dep_id = dep["id"]
        service_name = (dep.get("service") or {}).get("name", "unknown")

        log_entries = await self.railway.get_deployment_logs(dep_id)
        messages = [e.get("message", "") for e in log_entries if e.get("message")]

        if not messages:
            build_entries = await self.railway.get_build_logs(dep_id)
            messages = [e.get("message", "") for e in build_entries if e.get("message")]

        if not messages:
            return await interaction.followup.send(
                embed=status_embed(
                    "No Logs",
                    f"No logs available for `{dep_id[:12]}…` (status: `{dep['status']}`).",
                )
            )

        tail = messages[-lines:]
        severity = classify_log_batch(tail)

        embed = log_alert_embed(
            project["name"], service_name, env["name"], dep_id, severity, tail
        )
        embed.title = f"📋  Logs — {project['name']}"
        await interaction.followup.send(embed=embed)

    # ── /railway metrics ───────────────────────────────────────────────────────

    @railway_group.command(
        name="metrics",
        description="Check current CPU/memory usage for every service in a project",
    )
    @app_commands.describe(
        project_name="Name of the Railway project",
        environment="Environment name (default: production)",
    )
    async def metrics(
        self,
        interaction: discord.Interaction,
        project_name: str,
        environment: str = "production",
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            projects = await self.railway.get_projects()
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"```{e}```"))

        project = next(
            (p for p in projects if p["name"].lower() == project_name.lower()), None
        )
        if not project:
            names = ", ".join(f"`{p['name']}`" for p in projects)
            return await interaction.followup.send(
                embed=error_embed(f"Project `{project_name}` not found.\n\nAvailable: {names}")
            )

        env = next(
            (
                e["node"]
                for e in project["environments"]["edges"]
                if e["node"]["name"].lower() == environment.lower()
            ),
            None,
        )
        if not env:
            env_names = ", ".join(
                f"`{e['node']['name']}`" for e in project["environments"]["edges"]
            )
            return await interaction.followup.send(
                embed=error_embed(f"Environment `{environment}` not found.\n\nAvailable: {env_names}")
            )

        services = [e["node"] for e in project["services"]["edges"]]
        if not services:
            return await interaction.followup.send(
                embed=error_embed(f"No services found in **{project_name}**.")
            )

        pages = []
        for svc in services:
            data = await self.railway.get_service_metrics(svc["id"], env["id"])
            if not data:
                pages.append(
                    status_embed(
                        f"📊  {svc['name']}",
                        "No metrics available — service may not be running.",
                    )
                )
                continue

            cpu_pct = data.get("cpuPercentage") or 0.0
            mem_bytes = data.get("memoryUsageBytes") or 0
            mem_limit = data.get("memoryLimitBytes") or 0
            pages.append(
                metrics_embed(project["name"], svc["name"], env["name"], cpu_pct, mem_bytes, mem_limit)
            )

        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0])
        else:
            view = PaginatorView(pages, interaction.user.id)
            await interaction.followup.send(embed=pages[0], view=view)

    # ── /railway redeploy ─────────────────────────────────────────────────────

    @railway_group.command(
        name="redeploy",
        description="Trigger a redeploy for the latest deployment in a project (owner only)",
    )
    @app_commands.describe(
        project_name="Name of the Railway project",
        environment="Environment name (default: production)",
    )
    async def redeploy(
        self,
        interaction: discord.Interaction,
        project_name: str,
        environment: str = "production",
    ):
        if not is_owner(interaction):
            return await interaction.response.send_message(
                embed=error_embed("Only the bot owner can trigger redeploys."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        try:
            projects = await self.railway.get_projects()
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"```{e}```"))

        project = next(
            (p for p in projects if p["name"].lower() == project_name.lower()), None
        )
        if not project:
            return await interaction.followup.send(
                embed=error_embed(f"Project `{project_name}` not found.")
            )

        env = next(
            (
                e["node"]
                for e in project["environments"]["edges"]
                if e["node"]["name"].lower() == environment.lower()
            ),
            None,
        )
        if not env:
            return await interaction.followup.send(
                embed=error_embed(f"Environment `{environment}` not found.")
            )

        try:
            deps = await self.railway.get_recent_deployments(
                project["id"], env["id"], limit=1
            )
        except RailwayAPIError as e:
            return await interaction.followup.send(embed=error_embed(f"```{e}```"))

        if not deps:
            return await interaction.followup.send(
                embed=error_embed("No deployments found to redeploy.")
            )

        dep = deps[0]
        if not dep.get("canRedeploy", False):
            return await interaction.followup.send(
                embed=error_embed(
                    f"Deployment `{dep['id'][:12]}…` cannot be redeployed "
                    f"(status: `{dep['status']}`)."
                )
            )

        view = ConfirmRedeployView(
            interaction.user.id, self.railway, dep["id"], project["name"], environment
        )
        embed = discord.Embed(
            title="⚠️  Confirm Redeploy",
            description=(
                f"Redeploy **{project_name}** / **{environment}**?\n\n"
                f"Deployment: `{dep['id'][:12]}…`\n"
                f"Current status: `{dep['status']}`"
            ),
            color=discord.Color.orange(),
        )
        railway_footer(embed)
        await interaction.followup.send(embed=embed, view=view)


# ── CONFIRM VIEW ──────────────────────────────────────────────────────────────

class ConfirmRedeployView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        railway: RailwayClient,
        deployment_id: str,
        project_name: str,
        environment: str,
    ):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.railway = railway
        self.deployment_id = deployment_id
        self.project_name = project_name
        self.environment = environment

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=error_embed("Not your button."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, redeploy", style=discord.ButtonStyle.danger, emoji="🚀")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        success = await self.railway.redeploy(self.deployment_id)
        if success:
            await interaction.edit_original_response(
                embed=success_embed(
                    f"Redeploy triggered for **{self.project_name}** / **{self.environment}**.\n"
                    f"`{self.deployment_id[:12]}…`"
                ),
                view=None,
            )
        else:
            await interaction.edit_original_response(
                embed=error_embed("Redeploy failed. Check Railway dashboard."), view=None
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=status_embed("Cancelled", "Redeploy cancelled."), view=None
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── PAGINATOR ─────────────────────────────────────────────────────────────────

class PaginatorView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.current = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = max(0, self.current - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


async def setup(bot: commands.Bot):
    railway = bot.railway  # type: ignore[attr-defined]
    cog = RailwayCommands(bot, railway)
    if cfg.GUILD_ID and str(cfg.GUILD_ID).isdigit():
        guild = discord.Object(id=int(cfg.GUILD_ID))
        await bot.add_cog(cog, guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.add_cog(cog)
        await bot.tree.sync()
