"""Embed builders. Kept separate from trade logic so formatting changes
don't risk touching state-transition code."""

from __future__ import annotations

import discord


def trade_info_embed(trade: dict) -> discord.Embed:
    e = discord.Embed(title="Trade Information", color=discord.Color.blurple())
    e.add_field(name="User 1", value=f"<@{trade['user1_id']}>", inline=True)
    e.add_field(name="User 2", value=f"<@{trade['user2_id']}>", inline=True)
    e.add_field(name="\u200b", value="\u200b", inline=True)
    e.add_field(name="User 1 gives", value=f"`{trade['user1_gives']}`", inline=False)
    e.add_field(name="User 2 gives", value=f"`{trade['user2_gives']}`", inline=False)
    amount = trade.get("trade_value_usd")
    e.add_field(name="Dollar amount", value=f"`${amount}`" if amount is not None else "`Not entered yet`", inline=True)
    ltc = trade.get("ltc_amount")
    e.add_field(name="LTC amount", value=f"`{ltc} LTC`" if ltc and ltc > 0 else "`Not calculated yet`", inline=True)
    u1 = "✅" if trade["user1_confirmed"] else "⏳"
    u2 = "✅" if trade["user2_confirmed"] else "⏳"
    e.add_field(name="Confirmations", value=f"User 1: {u1}   User 2: {u2}", inline=False)
    e.set_footer(text=f"Trade #{trade['trade_id']}")
    return e


def sender_select_embed(trade: dict) -> discord.Embed:
    e = discord.Embed(
        title="Choose the LTC sender",
        description="Select which participant will send LTC. The other participant automatically becomes the receiver.",
        color=discord.Color.gold(),
    )
    e.add_field(name="Sender candidate — User 1", value=f"<@{trade['user1_id']}>", inline=True)
    e.add_field(name="Sender candidate — User 2", value=f"<@{trade['user2_id']}>", inline=True)
    e.set_footer(text=f"Trade #{trade['trade_id']}")
    return e


def deposit_waiting_embed(trade: dict) -> discord.Embed:
    e = discord.Embed(title="💰 LTC Deposit", color=discord.Color.orange())
    e.add_field(name="Dollar amount", value=f"${trade['trade_value_usd']}", inline=True)
    e.add_field(name="LTC amount", value=f"`{trade['ltc_amount']} LTC`", inline=True)
    e.add_field(name="LTC deposit address", value=f"`{trade['deposit_address']}`", inline=False)
    e.add_field(name="Status", value="Waiting for payment...", inline=False)
    e.set_footer(text=f"Trade #{trade['trade_id']} • Litecoin MAINNET")
    return e


def deposit_detected_embed(trade: dict, tx_id: str, amount: str, confirmations: int, required: int) -> discord.Embed:
    e = discord.Embed(title="💰 Payment detected", color=discord.Color.orange())
    e.add_field(name="LTC amount", value=f"`{amount} LTC`", inline=False)
    e.add_field(name="Transaction", value=f"`{tx_id}`", inline=False)
    e.add_field(name="Confirmations", value=f"`{confirmations}/{required}`", inline=False)
    e.set_footer(text=f"Trade #{trade['trade_id']} • Litecoin MAINNET")
    return e


def deposit_confirmed_embed(trade: dict, amount: str) -> discord.Embed:
    e = discord.Embed(title="✅ LTC deposit confirmed", color=discord.Color.green())
    e.add_field(name="LTC amount secured", value=f"`{amount} LTC`", inline=False)
    e.add_field(name="Transaction", value=f"`{trade['deposit_tx_id']}`", inline=False)
    e.set_footer(text=f"Trade #{trade['trade_id']}")
    return e


def trade_instructions_embed(trade: dict) -> discord.Embed:
    e = discord.Embed(title="📋 Trade Instructions", color=discord.Color.blurple())
    e.description = (
        f"1. Send the item to <@{trade['sender_id']}>.\n"
        f"2. Once <@{trade['sender_id']}> has received and verified the item, "
        f"they must click **Release LTC**.\n"
        f"3. <@{trade['receiver_id']}> will then enter an LTC address.\n"
        f"4. The escrow system will send the LTC to <@{trade['receiver_id']}>.\n"
        f"5. If the sender does not release the LTC, ping staff for assistance."
    )
    e.set_footer(text=f"Trade #{trade['trade_id']}")
    return e


def payout_address_preview_embed(trade: dict) -> discord.Embed:
    e = discord.Embed(title="Confirm payout address", color=discord.Color.gold())
    e.add_field(name="Dollar amount", value=f"${trade['trade_value_usd']}", inline=True)
    e.add_field(name="LTC amount", value=f"`{trade['ltc_amount']} LTC`", inline=True)
    e.add_field(name="Receiver LTC address", value=f"`{trade['receiver_payout_address']}`", inline=False)
    e.set_footer(text=f"Trade #{trade['trade_id']}")
    return e


def payout_sent_embed(trade: dict, tx_id: str, amount: object | None = None) -> discord.Embed:
    e = discord.Embed(title="💸 LTC sent", color=discord.Color.green())
    paid_amount = amount if amount is not None else trade["ltc_amount"]
    e.add_field(name="LTC amount", value=f"`{paid_amount} LTC`", inline=False)
    if trade.get("trade_value_usd") is not None:
        e.add_field(name="Dollar amount", value=f"${trade['trade_value_usd']}", inline=True)
    e.add_field(name="Transaction", value=f"`{tx_id}`", inline=False)
    e.set_footer(text=f"Trade #{trade['trade_id']} • COMPLETED")
    return e


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="❌ Error", description=message, color=discord.Color.red())
