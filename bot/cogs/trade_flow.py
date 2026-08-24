"""
Core trade flow: ticket creation -> info -> confirmation -> roles ->
deposit -> release -> payout address -> payout.

All button interactions are routed through a raw `on_interaction`
listener (see `route_component`) rather than relying on discord.py's
per-message View object staying alive in memory. This means every button
keeps working after a bot restart / redeploy on Railway, since we only
depend on the custom_id string, not any in-memory state.

Every handler re-fetches the trade row from the database and re-checks
permissions against the *real* interacting user's Discord ID before doing
anything -- button labels and cached embeds are never treated as a source
of truth.
"""

from __future__ import annotations

import logging
import re
import asyncio
import aiohttp
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import discord
from discord import app_commands
from discord.ext import commands

from config import CONFIG
from db.database import ConcurrencyError, Database, TradeNotFoundError
from ltc.blockcypher_client import BlockCypherClient, BlockCypherError, ltc_to_satoshis
from ltc.encryption import WalletEncryption
from utils import embeds
from utils.permissions import is_staff_member, is_trade_participant

logger = logging.getLogger("cogs.trade_flow")

REQUEST_LTC_CUSTOM_ID = "escrow:request_ltc"


def _cid(action: str, trade_id: int) -> str:
    return f"escrow:{action}:{trade_id}"


def _parse_cid(custom_id: str) -> tuple[str, int] | None:
    if not custom_id.startswith("escrow:"):
        return None
    parts = custom_id.split(":")
    if len(parts) != 3:
        return None
    _, action, trade_id_raw = parts
    try:
        return action, int(trade_id_raw)
    except ValueError:
        return None


class TradeInfoModal(discord.ui.Modal, title="Start a Trade"):
    other_username = discord.ui.TextInput(
        label="Username of the person you're trading with",
        placeholder="Username, display name, or Discord user ID",
        max_length=100,
    )
    you_give = discord.ui.TextInput(label="What is your item?", max_length=200)
    they_give = discord.ui.TextInput(label="What is their item?", max_length=200)

    def __init__(self, cog: "TradeFlowCog", trade_id: int | None):
        super().__init__()
        self.cog = cog
        self.trade_id = trade_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_trade_info_submitted(interaction, self.trade_id, self)


class ReenterItemsModal(discord.ui.Modal, title="Re-enter Trade Items"):
    you_give = discord.ui.TextInput(label="What is your item?", max_length=200)
    they_give = discord.ui.TextInput(label="What is their item?", max_length=200)

    def __init__(self, cog: "TradeFlowCog", trade_id: int):
        super().__init__()
        self.cog = cog
        self.trade_id = trade_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_items_reentered(interaction, self.trade_id, self)


class TradeAmountModal(discord.ui.Modal, title="Trade Amount"):
    amount = discord.ui.TextInput(
        label="Enter the number of dollars ($)",
        placeholder="e.g. 25",
        max_length=20,
    )

    def __init__(self, cog: "TradeFlowCog", trade_id: int):
        super().__init__()
        self.cog = cog
        self.trade_id = trade_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_amount_submitted(interaction, self.trade_id, str(self.amount.value))


class PayoutAddressModal(discord.ui.Modal, title="Enter LTC Payout Address"):
    address = discord.ui.TextInput(label="Your Litecoin (LTC) address", max_length=100)

    def __init__(self, cog: "TradeFlowCog", trade_id: int):
        super().__init__()
        self.cog = cog
        self.trade_id = trade_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_payout_address_submitted(interaction, self.trade_id, str(self.address.value))


class TradeFlowCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database, wallet_enc: WalletEncryption,
                 make_ltc_client):
        self.bot = bot
        self.db = db
        self.wallet_enc = wallet_enc
        self.make_ltc_client = make_ltc_client  # Callable[[], BlockCypherClient]

    # ------------------------------------------------------------------
    # Panel setup (staff/admin command to post the persistent button)
    # ------------------------------------------------------------------

    @app_commands.command(name="panelsteup", description="Post the Request LTC panel in this channel (staff only).")
    async def setup_escrow(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            await interaction.response.send_message("You do not have permission to do this.", ephemeral=True)
            return
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Request LTC", style=discord.ButtonStyle.success,
            custom_id=REQUEST_LTC_CUSTOM_ID, emoji="💱",
        ))
        embed = discord.Embed(
            title="LTC Middleman",
            description="Click below to open a private ticket and start a trade.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="release", description="Release a trade for a sender who did not press Release LTC (staff only).")
    @app_commands.describe(id="Trade ID shown when the trade starts")
    async def staff_release(self, interaction: discord.Interaction, id: int) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            await interaction.response.send_message("You do not have permission to do this.", ephemeral=True)
            return
        trade = await self.db.get_trade(id)
        if not trade:
            await interaction.response.send_message("No such trade.", ephemeral=True)
            return
        try:
            trade = await self.db.staff_release(id, interaction.user.id)
        except (ConcurrencyError, PermissionError) as exc:
            await interaction.response.send_message(f"Could not release Trade #{id}: {exc}", ephemeral=True)
            return

        channel = self.bot.get_channel(trade["channel_id"])
        if channel is not None:
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label="💰 Enter LTC Address", style=discord.ButtonStyle.success,
                custom_id=_cid("enter_address", id),
            ))
            await channel.send(
                content=f"<@{trade['receiver_id']}>",
                embed=discord.Embed(
                    title="Staff-assisted release",
                    description="Staff released this trade because the sender did not click Release LTC. "
                                "The receiver must now enter a Litecoin payout address.",
                    color=discord.Color.orange(),
                ),
                view=view,
            )
        await interaction.response.send_message(f"Trade #{id} released for staff assistance.", ephemeral=True)

    # ------------------------------------------------------------------
    # Raw component routing (persists across restarts)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

        if custom_id == REQUEST_LTC_CUSTOM_ID:
            await self.start_ticket(interaction)
            return

        parsed = _parse_cid(custom_id)
        if not parsed:
            return
        action, trade_id = parsed

        handlers = {
            "reenter_info": self.handle_reenter_info,
            "confirm": self.handle_confirm,
            "incorrect": self.handle_incorrect,
            "enter_amount": self.handle_enter_amount,
            "amount_incorrect": self.handle_amount_incorrect,
            "role_user1": self.handle_role_selection,
            "role_user2": self.handle_role_selection,
            "release": self.handle_release_click,
            "release_confirm": self.handle_release_confirm,
            "release_cancel": self.handle_release_cancel,
            "close_ticket": self.handle_close_ticket,
            "enter_address": self.handle_enter_address_click,
            "address_confirm": self.handle_address_confirm,
            "address_change": self.handle_address_change,
        }
        handler = handlers.get(action)
        if not handler:
            return
        try:
            if action in ("role_user1", "role_user2"):
                await handler(interaction, trade_id, action)
            else:
                await handler(interaction, trade_id)
        except TradeNotFoundError:
            await self._safe_error(interaction, "This trade no longer exists.")
        except ConcurrencyError as e:
            await self._safe_error(interaction, f"This action can't be completed right now: {e}")
        except PermissionError as e:
            await self._safe_error(interaction, str(e))
        except Exception:
            logger.exception("Unhandled error in component handler action=%s trade_id=%s", action, trade_id)
            await self._safe_error(interaction, "Something went wrong handling that action. Staff has been notified.")

    @staticmethod
    async def _safe_error(interaction: discord.Interaction, message: str) -> None:
        embed = embeds.error_embed(message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # 1. Request LTC -> ticket creation
    # ------------------------------------------------------------------

    async def start_ticket(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TradeInfoModal(self, trade_id=None))

    async def handle_trade_info_submitted(self, interaction: discord.Interaction,
                                           trade_id: int | None, modal: TradeInfoModal) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(embed=embeds.error_embed("This must be used in a server."), ephemeral=True)
            return

        other_id = await self._resolve_member(guild, str(modal.other_username.value))
        if other_id is None:
            await interaction.followup.send(
                embed=embeds.error_embed("I couldn't find that username in this server. Try their exact Discord username or user ID."),
                ephemeral=True,
            )
            return

        if other_id == interaction.user.id:
            await interaction.followup.send(embed=embeds.error_embed("You can't trade with yourself."), ephemeral=True)
            return

        other_member = guild.get_member(other_id)
        if other_member is None:
            try:
                other_member = await guild.fetch_member(other_id)
            except discord.NotFound:
                other_member = None
            except discord.HTTPException:
                other_member = None

        if other_member is None:
            await interaction.followup.send(
                embed=embeds.error_embed("That user is not currently in this server."), ephemeral=True
            )
            return

        await self.db.upsert_user(interaction.user.id)
        await self.db.upsert_user(other_member.id)

        if trade_id is None:
            staff_role = guild.get_role(CONFIG.staff_role_id)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                other_member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

            channel = await guild.create_text_channel(
                name=f"ticket-{interaction.user.name}"[:90],
                overwrites=overwrites,
                reason=f"LTC escrow ticket opened by {interaction.user.id}",
            )
            trade_id = await self.db.create_trade(
                guild_id=guild.id, channel_id=channel.id, creator_id=interaction.user.id,
                expires_minutes=CONFIG.trade_expiration_minutes,
            )
        else:
            trade_row = await self.db.get_trade(trade_id)
            channel = guild.get_channel(trade_row["channel_id"])

        await self.db.submit_trade_info(
            trade_id=trade_id, user1_id=interaction.user.id, user2_id=other_member.id,
            user1_gives=str(modal.you_give.value), user2_gives=str(modal.they_give.value),
            ltc_amount=Decimal("0"), actor_id=interaction.user.id,
        )
        trade = await self.db.get_trade(trade_id)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="✅ Confirm", style=discord.ButtonStyle.success,
                                         custom_id=_cid("confirm", trade_id)))
        view.add_item(discord.ui.Button(label="❌ Incorrect", style=discord.ButtonStyle.danger,
                                         custom_id=_cid("incorrect", trade_id)))
        await channel.send(
            content=f"<@{interaction.user.id}> <@{other_member.id}>",
            embed=embeds.trade_info_embed(trade), view=view,
        )
        await channel.send(
            embed=discord.Embed(
                title=f"Trade #{trade_id}",
                description="Use the button below to close this ticket when the trade is finished.",
                color=discord.Color.greyple(),
            ),
            view=self._close_ticket_view(trade_id),
        )
        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)

    async def handle_reenter_info(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["creator_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the ticket creator can re-enter trade details.")
            return
        await interaction.response.send_modal(ReenterItemsModal(self, trade_id))

    async def handle_items_reentered(self, interaction: discord.Interaction, trade_id: int,
                                     modal: ReenterItemsModal) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["creator_id"] != interaction.user.id:
            await interaction.followup.send(embed=embeds.error_embed("Only the ticket creator can re-enter the items."), ephemeral=True)
            return
        await self.db.submit_trade_info(
            trade_id, trade["user1_id"], trade["user2_id"],
            str(modal.you_give.value), str(modal.they_give.value), Decimal("0"),
            interaction.user.id,
        )
        trade = await self.db.get_trade(trade_id)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="✅ Confirm", style=discord.ButtonStyle.success,
                                        custom_id=_cid("confirm", trade_id)))
        view.add_item(discord.ui.Button(label="❌ Incorrect", style=discord.ButtonStyle.danger,
                                        custom_id=_cid("incorrect", trade_id)))
        await interaction.channel.send(content=f"<@{trade['user1_id']}> <@{trade['user2_id']}>",
                                        embed=embeds.trade_info_embed(trade), view=view)
        await interaction.followup.send("The items were updated and sent for confirmation.", ephemeral=True)

    async def _resolve_member(self, guild: discord.Guild, value: str):
        value = value.strip()
        digits = re.sub(r"[<@!>]", "", value)
        if digits.isdigit():
            try:
                member = guild.get_member(int(digits)) or await guild.fetch_member(int(digits))
                return member.id if member else None
            except (discord.NotFound, discord.HTTPException):
                return None
        needle = value.lower().lstrip("@")
        try:
            async for member in guild.fetch_members(limit=None):
                if needle in {member.name.lower(), member.display_name.lower(),
                              str(member).lower()}:
                    return member.id
        except discord.HTTPException:
            return None
        return None

    # ------------------------------------------------------------------
    # 2. Confirmation
    # ------------------------------------------------------------------

    async def handle_confirm(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or not is_trade_participant(trade, interaction.user.id):
            await self._safe_error(interaction, "Only trade participants can confirm.")
            return

        trade = await self.db.set_confirmation(trade_id, interaction.user.id)

        if trade["user1_confirmed"] and trade["user2_confirmed"]:
            if trade["status"] == "AWAITING_CONFIRMATION":
                await self.db.advance_to_amount_confirmation(trade_id, interaction.user.id)
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(label="💵 Enter dollar amount",
                                                style=discord.ButtonStyle.primary,
                                                custom_id=_cid("enter_amount", trade_id)))
                await interaction.response.edit_message(embed=embeds.trade_info_embed(trade), view=None)
                await interaction.channel.send(content=f"<@{trade['creator_id']}>",
                                                embed=discord.Embed(
                                                    title="Both users confirmed",
                                                    description="The ticket creator must now enter the dollar amount for this trade.",
                                                    color=discord.Color.gold()), view=view)
                return
            await self.db.advance_to_roles_selected(trade_id, interaction.user.id)
            trade = await self.db.get_trade(trade_id)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Sender: User 1", style=discord.ButtonStyle.primary,
                                             custom_id=_cid("role_user1", trade_id)))
            view.add_item(discord.ui.Button(label="Sender: User 2", style=discord.ButtonStyle.primary,
                                             custom_id=_cid("role_user2", trade_id)))
            await interaction.response.edit_message(embed=embeds.trade_info_embed(trade), view=None)
            await interaction.channel.send(embed=embeds.sender_select_embed(trade), view=view)
        else:
            await interaction.response.edit_message(embed=embeds.trade_info_embed(trade))

    async def handle_incorrect(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or not is_trade_participant(trade, interaction.user.id):
            await self._safe_error(interaction, "Only trade participants can flag this as incorrect.")
            return
        if trade["status"] == "AWAITING_AMOUNT_CONFIRMATION":
            await self.db.mark_amount_incorrect(trade_id, interaction.user.id)
            await interaction.response.edit_message(
                embed=discord.Embed(title="Amount marked incorrect",
                                     description="The ticket creator must enter the dollar amount again.",
                                     color=discord.Color.red()), view=None)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="💵 Re-enter dollar amount",
                                            style=discord.ButtonStyle.primary,
                                            custom_id=_cid("enter_amount", trade_id)))
            await interaction.channel.send(content=f"<@{trade['creator_id']}>", view=view)
            return
        await self.db.mark_incorrect(trade_id, interaction.user.id)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="📝 Re-enter Trade Details", style=discord.ButtonStyle.primary,
                                         custom_id=_cid("reenter_info", trade_id)))
        embed = discord.Embed(
            title="Trade information marked incorrect",
            description="The ticket creator needs to re-enter the trade details.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def handle_enter_amount(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["creator_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the ticket creator can enter the dollar amount.")
            return
        await interaction.response.send_modal(TradeAmountModal(self, trade_id))

    async def handle_amount_submitted(self, interaction: discord.Interaction, trade_id: int,
                                      raw_amount: str) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["creator_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the ticket creator can enter the dollar amount.")
            return
        try:
            amount = Decimal(raw_amount.strip().replace("$", "").replace(",", ""))
            if amount <= 0:
                raise InvalidOperation
        except InvalidOperation:
            await self._safe_error(interaction, "Enter a valid positive dollar amount, for example 25.")
            return
        try:
            async with self.make_ltc_client() as client:
                ltc_usd_rate = await client.get_ltc_usd_rate()
        except BlockCypherError:
            logger.exception("Could not fetch LTC/USD rate for trade %s", trade_id)
            await self._safe_error(interaction, "I couldn't fetch the current LTC price. Please try again.")
            return
        ltc_amount = (amount / ltc_usd_rate).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if ltc_amount <= 0:
            await self._safe_error(interaction, "That dollar amount is too small to convert to Litecoin.")
            return
        await self.db.submit_trade_amount(trade_id, amount, ltc_amount, interaction.user.id)
        trade = await self.db.get_trade(trade_id)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="✅ Confirm", style=discord.ButtonStyle.success,
                                        custom_id=_cid("confirm", trade_id)))
        view.add_item(discord.ui.Button(label="❌ Incorrect", style=discord.ButtonStyle.danger,
                                        custom_id=_cid("incorrect", trade_id)))
        await interaction.response.send_message(embed=embeds.trade_info_embed(trade), view=view)

    async def handle_amount_incorrect(self, interaction: discord.Interaction, trade_id: int) -> None:
        await self.handle_incorrect(interaction, trade_id)

    # ------------------------------------------------------------------
    # 3. Sender / receiver selection
    # ------------------------------------------------------------------

    async def handle_role_selection(self, interaction: discord.Interaction, trade_id: int, action: str) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or not is_trade_participant(trade, interaction.user.id):
            await self._safe_error(interaction, "Only trade participants can select roles.")
            return

        chosen_sender_id = trade["user1_id"] if action == "role_user1" else trade["user2_id"]
        other_id = trade["user2_id"] if action == "role_user1" else trade["user1_id"]

        await self.db.set_sender_receiver(trade_id, sender_id=chosen_sender_id, receiver_id=other_id,
                                           actor_id=interaction.user.id)
        trade = await self.db.get_trade(trade_id)

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Roles selected",
                description=f"Sender: <@{chosen_sender_id}>\nReceiver: <@{other_id}>",
                color=discord.Color.green(),
            ),
            view=None,
        )
        channel = interaction.channel
        if channel is None:
            return

        # Confirm the role mapping before making the external blockchain call.
        # This keeps the ticket responsive even if the provider is unavailable.
        await channel.send(
            content=f"<@{chosen_sender_id}>",
            embed=discord.Embed(
                title="Sender and receiver confirmed",
                description=(
                    f"Sender: <@{chosen_sender_id}>\n"
                    f"Receiver: <@{other_id}>\n\n"
                    "Preparing the Litecoin deposit address for the sender..."
                ),
                color=discord.Color.green(),
            ),
        )
        try:
            await self._create_deposit_address(channel, trade_id)
        except BlockCypherError:
            logger.exception("Could not create the deposit address for trade %s", trade_id)
            await channel.send(
                embed=embeds.error_embed(
                    "The sender/receiver roles were saved, but the Litecoin testnet provider "
                    "did not return a deposit address. Staff needs to retry the deposit setup."
                )
            )

    async def _create_deposit_address(self, channel: discord.abc.Messageable, trade_id: int) -> None:
        async with self.make_ltc_client() as client:
            generated = await client.generate_address()
            encrypted_key = self.wallet_enc.encrypt(generated.private_key_hex)
            await self.db.attach_deposit_address(trade_id, generated.address, encrypted_key, CONFIG.ltc_network)

            if CONFIG.webhook_public_base_url:
                callback_url = f"{CONFIG.webhook_public_base_url.rstrip('/')}/webhooks/blockcypher"
                try:
                    await client.create_address_webhook(generated.address, callback_url, confirmations=0)
                except Exception:
                    logger.exception("Failed to register BlockCypher webhook for %s; polling fallback will still catch it", generated.address)

        trade = await self.db.get_trade(trade_id)
        await channel.send(embed=embeds.deposit_waiting_embed(trade))

    # ------------------------------------------------------------------
    # Deposit notifications (called by DepositMonitor)
    # ------------------------------------------------------------------

    async def on_deposit_event(self, trade_id: int, kind: str, data: dict) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade:
            return
        channel = self.bot.get_channel(trade["channel_id"])
        if channel is None:
            return

        if kind in ("deposit_seen", "deposit_progress"):
            await channel.send(embed=embeds.deposit_detected_embed(
                trade, data["tx_id"], data["amount"], data["confirmations"], data["confirmations_required"]
            ))
        elif kind == "deposit_confirmed":
            await channel.send(embed=embeds.deposit_confirmed_embed(trade, data["amount"]))
            await self.db.advance_to_in_progress(trade_id)
            trade = await self.db.get_trade(trade_id)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="🔓 Release LTC", style=discord.ButtonStyle.success,
                                             custom_id=_cid("release", trade_id)))
            instructions = (
                "**instructions:**\n"
                "1. Send the item to the sender.\n"
                "2. Once the sender has received and verified the item, the sender must click “Release LTC.”\n"
                "3. The receiver will then click “Enter LTC Address” and provide their LTC address.\n"
                "4. The bot will send the LTC to the receiver’s provided address.\n"
                "5. The receiver cannot click “Release LTC.” Only the sender can release the LTC.\n"
                "6. If the sender does not click “Release LTC,” please ping our staff team and they will assist you shortly.\n"
                "7. If the receiver says you have to click “Release LTC” first, ping our staff; "
                "they may be banned for attempting to scam."
            )
            await channel.send(content=instructions, view=view)

    # ------------------------------------------------------------------
    # 8. Release LTC
    # ------------------------------------------------------------------

    @staticmethod
    def _close_ticket_view(trade_id: int, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="🔒 Close Ticket", style=discord.ButtonStyle.secondary,
            custom_id=_cid("close_ticket", trade_id), disabled=disabled,
        ))
        return view

    async def handle_close_ticket(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or not is_trade_participant(trade, interaction.user.id):
            await self._safe_error(interaction, "Only trade participants can close this ticket.")
            return
        if trade["status"] in (
            "DEPOSIT_DETECTED", "LTC_CONFIRMED", "TRADE_IN_PROGRESS",
            "RELEASE_REQUESTED", "AWAITING_PAYOUT_ADDRESS",
            "PAYOUT_ADDRESS_CONFIRMED", "LTC_SENT", "COMPLETED",
        ):
            await self._safe_error(
                interaction,
                "This ticket cannot be closed after LTC has entered escrow. Please contact staff.",
            )
            return
        if interaction.channel is not None and interaction.guild is not None:
            await interaction.channel.set_permissions(
                interaction.guild.default_role, view_channel=False,
                reason=f"Trade #{trade_id} ticket closed by {interaction.user.id}",
            )
            for participant_id in (trade["user1_id"], trade["user2_id"]):
                member = interaction.guild.get_member(participant_id)
                if member is not None:
                    await interaction.channel.set_permissions(
                        member, view_channel=False, send_messages=False,
                        reason=f"Trade #{trade_id} ticket closed",
                    )
        await interaction.response.send_message("Ticket closed.", ephemeral=True)

    async def handle_release_click(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade:
            await self._safe_error(interaction, "Trade not found.")
            return
        if trade["receiver_id"] == interaction.user.id:
            await self._safe_error(interaction, "❌ You are not authorized to release this trade.")
            return
        if trade["sender_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the sender can release the LTC.")
            return

        await self.db.request_release(trade_id, interaction.user.id)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Confirm Release", style=discord.ButtonStyle.danger,
                                         custom_id=_cid("release_confirm", trade_id)))
        view.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary,
                                         custom_id=_cid("release_cancel", trade_id)))
        await interaction.response.send_message(
            embed=discord.Embed(title="Are you sure you want to release the LTC?", color=discord.Color.gold()),
            view=view,
        )

    async def handle_release_confirm(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["sender_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the sender can confirm this release.")
            return
        await self.db.confirm_release(trade_id, interaction.user.id)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="💰 Enter LTC Address", style=discord.ButtonStyle.success,
                                         custom_id=_cid("enter_address", trade_id)))
        await interaction.response.edit_message(
            embed=discord.Embed(title="✅ Release confirmed. Waiting for receiver's payout address.",
                                 color=discord.Color.green()),
            view=None,
        )
        await interaction.channel.send(
            content=f"<@{trade['receiver_id']}>", view=view,
            embed=discord.Embed(description="Enter the address you'd like your LTC sent to.", color=discord.Color.blurple()),
        )

    async def handle_release_cancel(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["sender_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the sender can cancel this release.")
            return
        await self.db.cancel_release(trade_id, interaction.user.id)
        await interaction.response.edit_message(
            embed=discord.Embed(title="Release cancelled.", color=discord.Color.greyple()), view=None
        )

    # ------------------------------------------------------------------
    # 9. Receiver LTC address
    # ------------------------------------------------------------------

    async def handle_enter_address_click(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade:
            await self._safe_error(interaction, "Trade not found.")
            return
        if trade["sender_id"] == interaction.user.id:
            await self._safe_error(interaction, "The sender cannot set the payout address.")
            return
        if trade["receiver_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the receiver can enter a payout address.")
            return
        await interaction.response.send_modal(PayoutAddressModal(self, trade_id))

    async def handle_payout_address_submitted(self, interaction: discord.Interaction, trade_id: int,
                                               address: str) -> None:
        address = address.strip()
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["receiver_id"] != interaction.user.id:
            await interaction.response.send_message(embed=embeds.error_embed("Only the receiver can set the payout address."), ephemeral=True)
            return

        if not BlockCypherClient.validate_address_format(address, CONFIG.ltc_network):
            await interaction.response.send_message(
                embed=embeds.error_embed("That doesn't look like a valid Litecoin address for the configured network."),
                ephemeral=True,
            )
            return

        await self.db.set_payout_address(trade_id, interaction.user.id, address)
        trade = await self.db.get_trade(trade_id)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Confirm", style=discord.ButtonStyle.success,
                                         custom_id=_cid("address_confirm", trade_id)))
        view.add_item(discord.ui.Button(label="Change Address", style=discord.ButtonStyle.secondary,
                                         custom_id=_cid("address_change", trade_id)))
        await interaction.response.send_message(embed=embeds.payout_address_preview_embed(trade), view=view)

    async def handle_address_confirm(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["receiver_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the receiver can confirm the payout address.")
            return
        await self.db.confirm_payout_address(trade_id, interaction.user.id)
        await interaction.response.edit_message(
            embed=discord.Embed(title="✅ Payout address confirmed. Sending LTC...", color=discord.Color.green()),
            view=None,
        )
        await self._execute_payout(interaction.channel, trade_id)

    async def handle_address_change(self, interaction: discord.Interaction, trade_id: int) -> None:
        trade = await self.db.get_trade(trade_id)
        if not trade or trade["receiver_id"] != interaction.user.id:
            await self._safe_error(interaction, "Only the receiver can change the payout address.")
            return
        await interaction.response.send_modal(PayoutAddressModal(self, trade_id))

    # ------------------------------------------------------------------
    # 10. Payout execution
    # ------------------------------------------------------------------

    async def _execute_payout(self, channel: discord.abc.Messageable, trade_id: int) -> None:
        got_lock = await self.db.try_acquire_payout_lock(trade_id)
        if not got_lock:
            logger.warning("Payout already in flight for trade %s; skipping duplicate execution", trade_id)
            return

        broadcast_attempted = False
        try:
            trade = await self.db.get_trade(trade_id)
            payout_amount = await self.db.get_confirmed_deposit_amount(trade_id)
            if payout_amount <= 0:
                raise ValueError("No confirmed LTC balance is available for payout")
            deposit_satoshis = ltc_to_satoshis(payout_amount)
            amount_satoshis = deposit_satoshis - CONFIG.payout_fee_reserve_satoshis
            if amount_satoshis <= 0:
                raise ValueError("The confirmed deposit is too small to cover the network fee")
            payout_amount = Decimal(amount_satoshis) / Decimal(10**8)

            await self.db.begin_payout(trade_id, trade["receiver_payout_address"], payout_amount)

            encrypted_key = await self.db.get_wallet_private_key(trade_id)
            private_key_hex = self.wallet_enc.decrypt(encrypted_key)

            async with self.make_ltc_client() as client:
                # From this point onward a lost response can still mean the
                # provider accepted the transaction. Never classify a later
                # error as a safe-to-retry failure.
                broadcast_attempted = True
                tx_id = await client.send_payout(
                    from_address=trade["deposit_address"],
                    private_key_hex=private_key_hex,
                    to_address=trade["receiver_payout_address"],
                    amount_satoshis=amount_satoshis,
                )

            await self.db.complete_payout(trade_id, tx_id)
            try:
                await self.db.mark_completed(trade_id)
            except Exception:
                # The money is recorded as SENT and must not be sent again just
                # because the cosmetic terminal-state update failed.
                logger.exception("Payout sent but completion update failed for trade %s", trade_id)
            trade = await self.db.get_trade(trade_id)
            await channel.send(embed=embeds.payout_sent_embed(trade, tx_id, payout_amount))

        except (asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError) as e:
            # The request may have reached the provider even though the
            # response did not. Never tell the user to retry an ambiguous send.
            logger.exception("Payout outcome is unknown for trade %s", trade_id)
            await self.db.fail_payout(trade_id, str(e), ambiguous=True)
            await channel.send(embed=embeds.error_embed(
                f"The payout outcome for trade #{trade_id} could not be verified. "
                "It is locked for staff on-chain review; do not retry or release again."
            ))
        except Exception as e:
            logger.exception("Payout failed for trade %s", trade_id)
            await self.db.fail_payout(trade_id, str(e), ambiguous=broadcast_attempted)
            if broadcast_attempted:
                message = (
                    f"The payout outcome for trade #{trade_id} could not be verified. "
                    "It is locked for staff on-chain review; do not retry or release again."
                )
            else:
                message = (
                    f"The payout failed and has been logged for staff review. Trade #{trade_id} is NOT completed. "
                    "Please ping staff -- do not attempt to release again."
                )
            await channel.send(embed=embeds.error_embed(message))
        finally:
            await self.db.release_payout_lock(trade_id)


async def setup(bot: commands.Bot) -> None:
    # Actual instantiation with db/wallet_enc/make_ltc_client happens in main.py,
    # since those are runtime dependencies constructed at startup.
    pass
