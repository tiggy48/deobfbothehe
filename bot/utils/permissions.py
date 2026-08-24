"""
Server-side permission checks.

Every button/select callback must re-derive permissions from the database
row + the interaction's real Discord user id -- never from what a label or
previously-rendered embed claims. Discord UI state can be stale (cached
components, multiple clicks in flight) so this is the actual source of
truth enforcement layer.
"""

from __future__ import annotations

import discord

from config import CONFIG


def is_staff_member(member: discord.Member) -> bool:
    return any(role.id == CONFIG.staff_role_id for role in getattr(member, "roles", []))


def is_trade_participant(trade: dict, discord_id: int) -> bool:
    return discord_id in {trade.get("creator_id"), trade.get("user1_id"), trade.get("user2_id")}


def is_sender(trade: dict, discord_id: int) -> bool:
    return trade.get("sender_id") == discord_id


def is_receiver(trade: dict, discord_id: int) -> bool:
    return trade.get("receiver_id") == discord_id


class PermissionDenied(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PermissionDenied(message)
