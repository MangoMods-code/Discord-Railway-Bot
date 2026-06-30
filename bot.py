# bot.py — Entry point for Railway Monitor Bot.

import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands

import config as cfg
from railway_api import RailwayClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")


class RailwayMonitorBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self._session: aiohttp.ClientSession | None = None
        self.railway: RailwayClient | None = None

    async def setup_hook(self):
        self._session = aiohttp.ClientSession()
        self.railway = RailwayClient(cfg.RAILWAY_TOKEN, self._session)

        await self.load_extension("cogs.monitor")
        logger.info("  ✅ Loaded cogs.monitor")

        await self.load_extension("cogs.commands")
        logger.info("  ✅ Loaded cogs.commands")

    async def close(self):
        if self._session:
            await self._session.close()
        await super().close()

    async def on_ready(self):
        print("")
        print("╔══════════════════════════════════════════╗")
        print("║       🚂  Railway Monitor Online         ║")
        print(f"║  Logged in as {str(self.user)[:26].ljust(26)} ║")
        print(f"║  Poll interval: {str(cfg.POLL_INTERVAL) + 's':<25} ║")
        print("╚══════════════════════════════════════════╝")
        print("")

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ):
        from helpers import error_embed
        msg = str(error)
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(msg), ephemeral=True)


async def main():
    bot = RailwayMonitorBot()
    await bot.start(cfg.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
