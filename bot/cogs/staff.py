"""
Staff-only slash commands.

Every command re-checks `is_staff_member` server-side against the
invoking member's actual roles -- Discord's own slash command permission
UI is a convenience layer only and is not trusted as the sole gate here.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from db.database import ConcurrencyError, Database, TradeNotFoundError
from utils.permissions import is_staff_member


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


class StaffCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db

    trade_group = app_commands.Group(name="trade", description="Staff trade management commands")

    @trade_group.command(name="view", description="View a trade's current state.")
    @app_commands.describe(trade_id="The trade ID (shown in ticket embeds)")
    @staff_only()
    async def trade_view(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade:
            await interaction.response.send_message("No such trade.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Trade #{trade_id}", color=discord.Color.blurple())
        embed.add_field(name="Status", value=f"`{trade['status']}`", inline=True)
        embed.add_field(name="Locked", value="Yes" if trade["locked_by_staff"] else "No", inline=True)
        embed.add_field(name="Creator", value=f"<@{trade['creator_id']}>", inline=False)
        embed.add_field(name="User 1 / User 2", value=f"<@{trade['user1_id']}> / <@{trade['user2_id']}>", inline=False)
        embed.add_field(name="Sender / Receiver", value=f"<@{trade['sender_id']}> / <@{trade['receiver_id']}>", inline=False)
        embed.add_field(name="Dollar amount", value=f"`{trade.get('trade_value_usd')}`", inline=True)
        embed.add_field(name="LTC amount", value=f"`{trade['ltc_amount']} LTC`", inline=True)
        embed.add_field(name="LTC deposit address", value=f"`{trade['deposit_address']}`", inline=False)
        embed.add_field(name="Deposit TXID", value=f"`{trade['deposit_tx_id']}`", inline=False)
        embed.add_field(name="Deposit confirmations", value=f"`{trade['deposit_confirmations']}`", inline=True)
        embed.add_field(name="Payout address", value=f"`{trade['receiver_payout_address']}`", inline=False)
        embed.add_field(name="Payout TXID", value=f"`{trade['payout_tx_id']}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @trade_group.command(name="cancel", description="Cancel a trade.")
    @app_commands.describe(trade_id="The trade ID", reason="Reason for cancellation")
    @staff_only()
    async def trade_cancel(self, interaction: discord.Interaction, trade_id: int, reason: str) -> None:
        try:
            await self.db.staff_cancel_trade(trade_id, interaction.user.id, reason)
        except TradeNotFoundError:
            await interaction.response.send_message("No such trade.", ephemeral=True)
            return
        except ConcurrencyError as e:
            await interaction.response.send_message(f"Could not cancel: {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"Trade #{trade_id} cancelled.", ephemeral=True)

    @trade_group.command(name="lock", description="Lock a trade (prevents further user actions).")
    @app_commands.describe(trade_id="The trade ID")
    @staff_only()
    async def trade_lock(self, interaction: discord.Interaction, trade_id: int) -> None:
        try:
            await self.db.staff_set_lock(trade_id, interaction.user.id)
        except TradeNotFoundError:
            await interaction.response.send_message("No such trade.", ephemeral=True)
            return
        await interaction.response.send_message(f"Trade #{trade_id} locked.", ephemeral=True)

    @trade_group.command(name="unlock", description="Unlock a trade.")
    @app_commands.describe(trade_id="The trade ID")
    @staff_only()
    async def trade_unlock(self, interaction: discord.Interaction, trade_id: int) -> None:
        try:
            await self.db.staff_set_lock(trade_id, None)
        except TradeNotFoundError:
            await interaction.response.send_message("No such trade.", ephemeral=True)
            return
        await interaction.response.send_message(f"Trade #{trade_id} unlocked.", ephemeral=True)

    @trade_group.command(name="dispute", description="Mark a trade as disputed.")
    @app_commands.describe(trade_id="The trade ID")
    @staff_only()
    async def trade_dispute(self, interaction: discord.Interaction, trade_id: int) -> None:
        try:
            await self.db.staff_open_dispute(trade_id, interaction.user.id)
        except TradeNotFoundError:
            await interaction.response.send_message("No such trade.", ephemeral=True)
            return
        except ConcurrencyError as e:
            await interaction.response.send_message(f"Could not open dispute: {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"Trade #{trade_id} marked as disputed.", ephemeral=True)

    @trade_group.command(name="audit-log", description="View the audit log for a trade.")
    @app_commands.describe(trade_id="The trade ID")
    @staff_only()
    async def trade_audit_log(self, interaction: discord.Interaction, trade_id: int) -> None:
        events = await self.db.get_audit_log(trade_id)
        if not events:
            await interaction.response.send_message("No events logged for that trade.", ephemeral=True)
            return
        lines = []
        for ev in events[-25:]:
            actor = f"<@{ev['actor_id']}>" if ev["actor_id"] else "system"
            lines.append(f"`{ev['created_at']:%Y-%m-%d %H:%M:%S}` **{ev['event_type']}** by {actor} "
                         f"({ev['from_status']} → {ev['to_status']})")
        embed = discord.Embed(title=f"Audit log — Trade #{trade_id}", description="\n".join(lines),
                               color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)
