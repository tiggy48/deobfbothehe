"""
Entrypoint. Wires together:

  Discord bot  <->  Database (Postgres)  <->  LTC blockchain monitoring  <->  Escrow state  <->  Payout

Run locally with `python main.py` after populating a `.env` file (see
.env.example). On Railway, environment variables are injected directly and
this same entrypoint is used as the start command.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from aiohttp import web
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is a dev convenience only; not required in production (Railway injects env vars)

from config import CONFIG
from db.database import Database
from ltc.blockcypher_client import BlockCypherClient
from ltc.encryption import WalletEncryption
from ltc.monitor import DepositMonitor
from cogs.trade_flow import TradeFlowCog, REQUEST_LTC_CUSTOM_ID
from cogs.staff import StaffCog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

intents = discord.Intents.default()
# Member lookups use Discord's explicit fetch_member endpoint, so the bot does
# not need the privileged Server Members gateway intent just to run.
intents.members = False
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)

db = Database(CONFIG.database_url)
wallet_enc = WalletEncryption(CONFIG.wallet_encryption_key)


def make_ltc_client() -> BlockCypherClient:
    return BlockCypherClient(
        base_url=CONFIG.blockcypher_base_url,
        network=CONFIG.ltc_network,
        api_token=CONFIG.ltc_api_token,
    )


monitor = DepositMonitor(
    db=db,
    make_client=make_ltc_client,
    confirmations_required=CONFIG.confirmations_required,
    poll_interval_seconds=CONFIG.poll_interval_seconds,
)

trade_flow_cog: TradeFlowCog | None = None


@tasks.loop(minutes=5)
async def expire_trades_loop():
    try:
        expired = await db.expire_stale_trades()
        if expired:
            logger.info("Expired stale trades: %s", expired)
    except Exception:
        logger.exception("Error expiring stale trades")


@bot.event
async def on_ready():
    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    # Re-register the persistent top-level button so it keeps working after restarts.
    persistent_view = discord.ui.View(timeout=None)
    persistent_view.add_item(discord.ui.Button(
        label="Request LTC", style=discord.ButtonStyle.success,
        custom_id=REQUEST_LTC_CUSTOM_ID, emoji="💱",
    ))
    bot.add_view(persistent_view)

    try:
        if CONFIG.guild_id:
            guild = discord.Object(id=CONFIG.guild_id)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        logger.info("Slash commands synced.")
    except Exception:
        logger.exception("Failed to sync slash commands")


async def build_webhook_app() -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def blockcypher_webhook(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        try:
            await monitor.handle_webhook_event(payload)
        except Exception:
            logger.exception("Error processing BlockCypher webhook payload")
            # Still 200 -- BlockCypher will retry on non-2xx, and the polling
            # fallback will pick this up regardless.
        return web.json_response({"status": "received"})

    app.router.add_get("/health", health)
    app.router.add_post("/webhooks/blockcypher", blockcypher_webhook)
    return app


async def main() -> None:
    global trade_flow_cog

    await db.connect()
    logger.info("Database connected and schema ensured.")

    trade_flow_cog = TradeFlowCog(bot, db, wallet_enc, make_ltc_client)
    monitor.set_notify_callback(trade_flow_cog.on_deposit_event)

    await bot.add_cog(trade_flow_cog)
    await bot.add_cog(StaffCog(bot, db))

    monitor.start()
    expire_trades_loop.start()

    app = await build_webhook_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG.webhook_host, CONFIG.webhook_port)
    await site.start()
    logger.info("Webhook server listening on %s:%s", CONFIG.webhook_host, CONFIG.webhook_port)

    try:
        await bot.start(CONFIG.discord_bot_token)
    finally:
        await monitor.stop()
        expire_trades_loop.cancel()
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
