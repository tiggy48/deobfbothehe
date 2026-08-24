"""
Async database access layer (asyncpg).

All trade-state-changing operations go through this module so that we get
consistent row locking (SELECT ... FOR UPDATE) and a single place that
writes to trade_events for auditing. Cogs should never write raw SQL
themselves.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from utils.state import TradeState

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


class TradeNotFoundError(Exception):
    pass


class ConcurrencyError(Exception):
    """Raised when an operation could not be safely completed due to
    concurrent modification (e.g. a payout already in flight)."""


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=10)
        await self._init_schema()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def _init_schema(self) -> None:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        async with self.pool.acquire() as conn:
            await conn.execute(schema_sql)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def upsert_user(self, discord_id: int, is_staff: bool = False) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (discord_id, is_staff)
                VALUES ($1, $2)
                ON CONFLICT (discord_id) DO UPDATE
                    SET last_seen_at = now(),
                        is_staff = users.is_staff OR EXCLUDED.is_staff
                """,
                discord_id,
                is_staff,
            )

    # ------------------------------------------------------------------
    # Trades - creation & lookups
    # ------------------------------------------------------------------

    async def create_trade(self, guild_id: int, channel_id: int, creator_id: int,
                            expires_minutes: int) -> int:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO trades (guild_id, channel_id, creator_id, status, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING trade_id
                """,
                guild_id, channel_id, creator_id, TradeState.CREATED.value, expires_at,
            )
            trade_id = row["trade_id"]
            await self._log_event(conn, trade_id, creator_id, "TRADE_CREATED", None, TradeState.CREATED.value)
            return trade_id

    async def get_trade(self, trade_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trades WHERE trade_id = $1", trade_id)
            return dict(row) if row else None

    async def get_trade_by_channel(self, channel_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trades WHERE channel_id = $1", channel_id)
            return dict(row) if row else None

    async def get_trade_by_deposit_address(self, address: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trades WHERE deposit_address = $1", address)
            return dict(row) if row else None

    async def list_active_trades(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM trades
                WHERE status NOT IN ('COMPLETED','CANCELLED','FAILED','EXPIRED')
                ORDER BY created_at ASC
                """
            )
            return [dict(r) for r in rows]

    async def list_awaiting_deposit_trades(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM trades
                WHERE status IN ('AWAITING_DEPOSIT', 'DEPOSIT_DETECTED')
                  AND deposit_address IS NOT NULL
                """
            )
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Trade info / confirmation flow
    # ------------------------------------------------------------------

    async def submit_trade_info(self, trade_id: int, user1_id: int, user2_id: int,
                                 user1_gives: str, user2_gives: str, ltc_amount: Decimal,
                                 actor_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.CREATED, TradeState.AWAITING_CONFIRMATION,
                                             TradeState.INFORMATION_SUBMITTED})
                await conn.execute(
                    """
                    UPDATE trades
                    SET user1_id = $2, user2_id = $3, user1_gives = $4, user2_gives = $5,
                        ltc_amount = $6, trade_value_usd = NULL,
                        user1_confirmed = FALSE, user2_confirmed = FALSE,
                        status = $7, updated_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, user1_id, user2_id, user1_gives, user2_gives, ltc_amount,
                    TradeState.AWAITING_CONFIRMATION.value,
                )
                await self._log_event(conn, trade_id, actor_id, "INFO_SUBMITTED",
                                       trade["status"], TradeState.AWAITING_CONFIRMATION.value,
                                       {"user1_gives": user1_gives, "user2_gives": user2_gives,
                                        "ltc_amount": str(ltc_amount)})

    async def set_confirmation(self, trade_id: int, discord_id: int) -> dict:
        """Mark the given user's confirmation. Returns the updated trade row.
        Both users confirming is checked by the caller (cog) to decide
        whether to advance to ROLES_SELECTED."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_CONFIRMATION,
                                             TradeState.AWAITING_AMOUNT_CONFIRMATION})
                if discord_id == trade["user1_id"]:
                    await conn.execute("UPDATE trades SET user1_confirmed = TRUE, updated_at = now() WHERE trade_id = $1", trade_id)
                elif discord_id == trade["user2_id"]:
                    await conn.execute("UPDATE trades SET user2_confirmed = TRUE, updated_at = now() WHERE trade_id = $1", trade_id)
                else:
                    raise PermissionError("User is not a participant of this trade")
                await self._log_event(conn, trade_id, discord_id, "USER_CONFIRMED", trade["status"], trade["status"])
                row = await conn.fetchrow("SELECT * FROM trades WHERE trade_id = $1", trade_id)
                return dict(row)

    async def advance_to_amount_confirmation(self, trade_id: int, actor_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_CONFIRMATION})
                if not (trade["user1_confirmed"] and trade["user2_confirmed"]):
                    raise ConcurrencyError("Both users must confirm before entering the amount")
                await conn.execute(
                    "UPDATE trades SET status = $2, user1_confirmed = FALSE, user2_confirmed = FALSE, "
                    "updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.AWAITING_AMOUNT_CONFIRMATION.value,
                )
                await self._log_event(conn, trade_id, actor_id, "INFO_CONFIRMED",
                                       trade["status"], TradeState.AWAITING_AMOUNT_CONFIRMATION.value)

    async def submit_trade_amount(self, trade_id: int, usd_amount: Decimal,
                                  ltc_amount: Decimal, actor_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_AMOUNT_CONFIRMATION})
                await conn.execute(
                    "UPDATE trades SET ltc_amount = $2, trade_value_usd = $3, "
                    "user1_confirmed = FALSE, user2_confirmed = FALSE, updated_at = now() "
                    "WHERE trade_id = $1",
                    trade_id, ltc_amount, usd_amount,
                )
                await self._log_event(conn, trade_id, actor_id, "AMOUNT_SUBMITTED",
                                       trade["status"], trade["status"],
                                       {"usd_amount": str(usd_amount), "ltc_amount": str(ltc_amount)})

    async def mark_amount_incorrect(self, trade_id: int, actor_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_AMOUNT_CONFIRMATION})
                await conn.execute(
                    "UPDATE trades SET ltc_amount = 0, trade_value_usd = NULL, "
                    "user1_confirmed = FALSE, user2_confirmed = FALSE, updated_at = now() "
                    "WHERE trade_id = $1", trade_id,
                )
                await self._log_event(conn, trade_id, actor_id, "AMOUNT_MARKED_INCORRECT",
                                       trade["status"], trade["status"])

    async def mark_incorrect(self, trade_id: int, actor_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_CONFIRMATION})
                await conn.execute(
                    """
                    UPDATE trades
                    SET user1_confirmed = FALSE, user2_confirmed = FALSE,
                        status = $2, updated_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, TradeState.INFORMATION_SUBMITTED.value,
                )
                await self._log_event(conn, trade_id, actor_id, "MARKED_INCORRECT",
                                       trade["status"], TradeState.INFORMATION_SUBMITTED.value)

    async def advance_to_roles_selected(self, trade_id: int, actor_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_AMOUNT_CONFIRMATION})
                if not (trade["user1_confirmed"] and trade["user2_confirmed"]):
                    raise ConcurrencyError("Both users must confirm before advancing")
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.ROLES_SELECTED.value,
                )
                await self._log_event(conn, trade_id, actor_id, "BOTH_CONFIRMED",
                                       trade["status"], TradeState.ROLES_SELECTED.value)

    async def set_sender_receiver(self, trade_id: int, sender_id: int, receiver_id: int, actor_id: int) -> None:
        if sender_id == receiver_id:
            raise ValueError("Sender and receiver cannot be the same user")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.ROLES_SELECTED})
                valid_ids = {trade["user1_id"], trade["user2_id"]}
                if sender_id not in valid_ids or receiver_id not in valid_ids:
                    raise PermissionError("Sender/receiver must be trade participants")
                await conn.execute(
                    """
                    UPDATE trades
                    SET sender_id = $2, receiver_id = $3, status = $4, updated_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, sender_id, receiver_id, TradeState.AWAITING_DEPOSIT.value,
                )
                await self._log_event(conn, trade_id, actor_id, "ROLES_SET",
                                       trade["status"], TradeState.AWAITING_DEPOSIT.value,
                                       {"sender_id": sender_id, "receiver_id": receiver_id})

    # ------------------------------------------------------------------
    # Deposit address / monitoring
    # ------------------------------------------------------------------

    async def attach_deposit_address(self, trade_id: int, address: str, encrypted_private_key: str,
                                      network: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_DEPOSIT})
                wallet_row = await conn.fetchrow(
                    """
                    INSERT INTO wallet_addresses (trade_id, address, encrypted_private_key, network)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    trade_id, address, encrypted_private_key, network,
                )
                await conn.execute(
                    "UPDATE trades SET deposit_address = $2, deposit_address_index = $3, updated_at = now() WHERE trade_id = $1",
                    trade_id, address, wallet_row["id"],
                )
                await self._log_event(conn, trade_id, None, "DEPOSIT_ADDRESS_CREATED",
                                       trade["status"], trade["status"], {"address": address})

    async def get_wallet_private_key(self, trade_id: int) -> Optional[str]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT encrypted_private_key FROM wallet_addresses WHERE trade_id = $1", trade_id
            )
            return row["encrypted_private_key"] if row else None

    async def get_confirmed_deposit_amount(self, trade_id: int) -> Decimal:
        """Return the total on-chain amount observed for this escrow trade."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount), 0) AS amount FROM deposits WHERE trade_id = $1",
                trade_id,
            )
            return Decimal(str(row["amount"]))

    async def record_deposit_seen(self, trade_id: int, tx_id: str, address: str, amount: Decimal,
                                   confirmations: int) -> dict:
        """Idempotently record/update a deposit observation. Returns the updated trade row."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                if trade["status"] not in (TradeState.AWAITING_DEPOSIT.value, TradeState.DEPOSIT_DETECTED.value):
                    # Ignore late/duplicate notifications once we've moved on.
                    return trade

                await conn.execute(
                    """
                    INSERT INTO deposits (trade_id, tx_id, address, amount, confirmations, confirmed_at)
                    VALUES ($1, $2, $3, $4, $5, CASE WHEN $5 > 0 THEN now() ELSE NULL END)
                    ON CONFLICT (trade_id, tx_id) DO UPDATE
                        SET confirmations = EXCLUDED.confirmations,
                            confirmed_at = COALESCE(deposits.confirmed_at, EXCLUDED.confirmed_at)
                    """,
                    trade_id, tx_id, address, amount, confirmations,
                )

                new_status = trade["status"]
                if trade["status"] == TradeState.AWAITING_DEPOSIT.value:
                    new_status = TradeState.DEPOSIT_DETECTED.value

                await conn.execute(
                    """
                    UPDATE trades
                    SET deposit_tx_id = $2, deposit_confirmations = $3, status = $4, updated_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, tx_id, confirmations, new_status,
                )
                await self._log_event(conn, trade_id, None, "DEPOSIT_SEEN", trade["status"], new_status,
                                       {"tx_id": tx_id, "amount": str(amount), "confirmations": confirmations})
                row = await conn.fetchrow("SELECT * FROM trades WHERE trade_id = $1", trade_id)
                return dict(row)

    async def confirm_deposit(self, trade_id: int) -> dict:
        """Move DEPOSIT_DETECTED -> LTC_CONFIRMED once enough confirmations exist.
        Safe to call repeatedly; returns current trade row either way."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                if trade["status"] != TradeState.DEPOSIT_DETECTED.value:
                    return trade
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.LTC_CONFIRMED.value,
                )
                await self._log_event(conn, trade_id, None, "DEPOSIT_CONFIRMED",
                                       trade["status"], TradeState.LTC_CONFIRMED.value)
                row = await conn.fetchrow("SELECT * FROM trades WHERE trade_id = $1", trade_id)
                return dict(row)

    async def advance_to_in_progress(self, trade_id: int, actor_id: Optional[int] = None) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.LTC_CONFIRMED})
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.TRADE_IN_PROGRESS.value,
                )
                await self._log_event(conn, trade_id, actor_id, "TRADE_IN_PROGRESS",
                                       trade["status"], TradeState.TRADE_IN_PROGRESS.value)

    # ------------------------------------------------------------------
    # Release / payout
    # ------------------------------------------------------------------

    async def request_release(self, trade_id: int, sender_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.TRADE_IN_PROGRESS})
                if trade["sender_id"] != sender_id:
                    raise PermissionError("Only the sender may release the LTC")
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.RELEASE_REQUESTED.value,
                )
                await self._log_event(conn, trade_id, sender_id, "RELEASE_REQUESTED",
                                       trade["status"], TradeState.RELEASE_REQUESTED.value)

    async def confirm_release(self, trade_id: int, sender_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.RELEASE_REQUESTED})
                if trade["sender_id"] != sender_id:
                    raise PermissionError("Only the sender may confirm release")
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.AWAITING_PAYOUT_ADDRESS.value,
                )
                await self._log_event(conn, trade_id, sender_id, "RELEASE_CONFIRMED",
                                       trade["status"], TradeState.AWAITING_PAYOUT_ADDRESS.value)

    async def staff_release(self, trade_id: int, staff_id: int) -> dict:
        """Let verified staff advance a stalled release without impersonating
        the sender. This still requires the trade to be in a release-eligible
        state and records the staff actor in the audit log."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(
                    trade, {TradeState.TRADE_IN_PROGRESS, TradeState.RELEASE_REQUESTED}
                )
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.AWAITING_PAYOUT_ADDRESS.value,
                )
                await self._log_event(
                    conn, trade_id, staff_id, "STAFF_RELEASED",
                    trade["status"], TradeState.AWAITING_PAYOUT_ADDRESS.value,
                )
                row = await conn.fetchrow("SELECT * FROM trades WHERE trade_id = $1", trade_id)
                return dict(row)

    async def cancel_release(self, trade_id: int, sender_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.RELEASE_REQUESTED})
                if trade["sender_id"] != sender_id:
                    raise PermissionError("Only the sender may cancel their own release request")
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.TRADE_IN_PROGRESS.value,
                )
                await self._log_event(conn, trade_id, sender_id, "RELEASE_CANCELLED",
                                       trade["status"], TradeState.TRADE_IN_PROGRESS.value)

    async def set_payout_address(self, trade_id: int, receiver_id: int, address: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_PAYOUT_ADDRESS})
                if trade["receiver_id"] != receiver_id:
                    raise PermissionError("Only the receiver may set the payout address")
                await conn.execute(
                    "UPDATE trades SET receiver_payout_address = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, address,
                )
                await self._log_event(conn, trade_id, receiver_id, "PAYOUT_ADDRESS_SET",
                                       trade["status"], trade["status"], {"address": address})

    async def confirm_payout_address(self, trade_id: int, receiver_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.AWAITING_PAYOUT_ADDRESS})
                if trade["receiver_id"] != receiver_id:
                    raise PermissionError("Only the receiver may confirm the payout address")
                if not trade["receiver_payout_address"]:
                    raise ValueError("No payout address has been set yet")
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.PAYOUT_ADDRESS_CONFIRMED.value,
                )
                await self._log_event(conn, trade_id, receiver_id, "PAYOUT_ADDRESS_CONFIRMED",
                                       trade["status"], TradeState.PAYOUT_ADDRESS_CONFIRMED.value)

    async def try_acquire_payout_lock(self, trade_id: int) -> bool:
        """Best-effort lock so only one payout can be in flight for a trade
        at a time, even under concurrent interaction handling."""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO payout_locks (trade_id) VALUES ($1)", trade_id
                )
                return True
            except asyncpg.UniqueViolationError:
                return False

    async def release_payout_lock(self, trade_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM payout_locks WHERE trade_id = $1", trade_id)

    async def begin_payout(self, trade_id: int, destination_address: str, amount: Decimal) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.PAYOUT_ADDRESS_CONFIRMED})
                existing = await conn.fetchrow("SELECT * FROM payouts WHERE trade_id = $1", trade_id)
                if existing is not None:
                    raise ConcurrencyError("A payout already exists for this trade")
                row = await conn.fetchrow(
                    """
                    INSERT INTO payouts (trade_id, destination_address, amount, status)
                    VALUES ($1, $2, $3, 'PENDING')
                    RETURNING id
                    """,
                    trade_id, destination_address, amount,
                )
                await self._log_event(conn, trade_id, None, "PAYOUT_STARTED", trade["status"], trade["status"],
                                       {"destination": destination_address, "amount": str(amount)})
                return row["id"]

    async def complete_payout(self, trade_id: int, tx_id: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.PAYOUT_ADDRESS_CONFIRMED})
                payout = await conn.fetchrow(
                    "SELECT status FROM payouts WHERE trade_id = $1 FOR UPDATE", trade_id
                )
                if payout is None:
                    raise ConcurrencyError("Cannot complete a payout that was never started")
                if payout["status"] != "PENDING":
                    raise ConcurrencyError(
                        f"Cannot complete payout in status {payout['status']}"
                    )
                await conn.execute(
                    """
                    UPDATE payouts
                    SET tx_id = $2, status = 'SENT', failure_reason = NULL, completed_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, tx_id,
                )
                await conn.execute(
                    """
                    UPDATE trades SET payout_tx_id = $2, status = $3, updated_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, tx_id, TradeState.LTC_SENT.value,
                )
                await self._log_event(conn, trade_id, None, "PAYOUT_SENT",
                                       trade["status"], TradeState.LTC_SENT.value, {"tx_id": tx_id})

    async def fail_payout(self, trade_id: int, reason: str, ambiguous: bool = False) -> None:
        """Record a payout failure without making an ambiguous send retryable.

        A timeout after the provider accepts a signed transaction is not proof
        that no transaction was broadcast. Such cases must be reconciled on
        chain by staff before any further spend is attempted.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                await conn.execute(
                    """
                    UPDATE payouts
                    SET status = $2, failure_reason = $3
                    WHERE trade_id = $1 AND status = 'PENDING'
                    """,
                    trade_id, "UNKNOWN_REVIEW" if ambiguous else "FAILED", reason,
                )
                await self._log_event(
                    conn, trade_id, None,
                    "PAYOUT_UNKNOWN_REVIEW" if ambiguous else "PAYOUT_FAILED",
                    trade["status"], trade["status"], {"reason": reason},
                )

    async def mark_completed(self, trade_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                self._require_state(trade, {TradeState.LTC_SENT})
                await conn.execute(
                    "UPDATE trades SET status = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, TradeState.COMPLETED.value,
                )
                await self._log_event(conn, trade_id, None, "TRADE_COMPLETED",
                                       trade["status"], TradeState.COMPLETED.value)

    # ------------------------------------------------------------------
    # Staff actions
    # ------------------------------------------------------------------

    async def staff_cancel_trade(self, trade_id: int, staff_id: int, reason: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                if trade["status"] in ("COMPLETED", "CANCELLED", "FAILED", "EXPIRED"):
                    raise ConcurrencyError("Trade is already in a terminal state")
                await conn.execute(
                    "UPDATE trades SET status = 'CANCELLED', updated_at = now() WHERE trade_id = $1",
                    trade_id,
                )
                await self._log_event(conn, trade_id, staff_id, "STAFF_CANCELLED",
                                       trade["status"], "CANCELLED", {"reason": reason})

    async def staff_set_lock(self, trade_id: int, staff_id: Optional[int]) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                await conn.execute(
                    "UPDATE trades SET locked_by_staff = $2, updated_at = now() WHERE trade_id = $1",
                    trade_id, staff_id,
                )
                await self._log_event(conn, trade_id, staff_id,
                                       "STAFF_LOCKED" if staff_id else "STAFF_UNLOCKED",
                                       trade["status"], trade["status"])

    async def staff_open_dispute(self, trade_id: int, staff_or_user_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await self._lock_trade(conn, trade_id)
                if trade["status"] in ("COMPLETED", "CANCELLED", "FAILED", "EXPIRED"):
                    raise ConcurrencyError("Trade is already in a terminal state")
                await conn.execute(
                    """
                    UPDATE trades SET status = 'DISPUTED', dispute_opened_by = $2, updated_at = now()
                    WHERE trade_id = $1
                    """,
                    trade_id, staff_or_user_id,
                )
                await self._log_event(conn, trade_id, staff_or_user_id, "DISPUTE_OPENED",
                                       trade["status"], "DISPUTED")

    async def get_audit_log(self, trade_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trade_events WHERE trade_id = $1 ORDER BY created_at ASC", trade_id
            )
            return [dict(r) for r in rows]

    async def expire_stale_trades(self) -> list[int]:
        """Called periodically. Expires any non-terminal trade past its
        expires_at, EXCEPT trades where funds are already escrowed
        (DEPOSIT_DETECTED or later) -- those must be resolved by staff, not
        silently expired, since money is already in the escrow address."""
        FUNDS_HELD_STATES = (
            TradeState.DEPOSIT_DETECTED.value, TradeState.LTC_CONFIRMED.value,
            TradeState.TRADE_IN_PROGRESS.value, TradeState.RELEASE_REQUESTED.value,
            TradeState.AWAITING_PAYOUT_ADDRESS.value, TradeState.PAYOUT_ADDRESS_CONFIRMED.value,
            TradeState.LTC_SENT.value,
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT trade_id, status FROM trades
                WHERE expires_at < now()
                  AND status NOT IN ('COMPLETED','CANCELLED','FAILED','EXPIRED')
                  AND status <> ALL($1::text[])
                """,
                list(FUNDS_HELD_STATES),
            )
            expired_ids = []
            for row in rows:
                async with conn.transaction():
                    trade = await self._lock_trade(conn, row["trade_id"])
                    if trade["status"] in FUNDS_HELD_STATES or trade["status"] in (
                        "COMPLETED", "CANCELLED", "FAILED", "EXPIRED",
                    ):
                        continue
                    await conn.execute(
                        "UPDATE trades SET status = 'EXPIRED', updated_at = now() WHERE trade_id = $1",
                        row["trade_id"],
                    )
                    await self._log_event(conn, row["trade_id"], None, "TRADE_EXPIRED",
                                           trade["status"], "EXPIRED")
                    expired_ids.append(row["trade_id"])
            return expired_ids

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _lock_trade(conn: asyncpg.Connection, trade_id: int) -> dict:
        row = await conn.fetchrow("SELECT * FROM trades WHERE trade_id = $1 FOR UPDATE", trade_id)
        if row is None:
            raise TradeNotFoundError(f"Trade {trade_id} does not exist")
        return dict(row)

    @staticmethod
    def _require_state(trade: dict, allowed: set[TradeState]) -> None:
        current = TradeState(trade["status"])
        if current not in allowed:
            raise ConcurrencyError(
                f"Trade {trade['trade_id']} is in state {current}, expected one of {sorted(s.value for s in allowed)}"
            )

    @staticmethod
    async def _log_event(conn: asyncpg.Connection, trade_id: int, actor_id: Optional[int],
                          event_type: str, from_status: Optional[str], to_status: Optional[str],
                          detail: Optional[dict[str, Any]] = None) -> None:
        await conn.execute(
            """
            INSERT INTO trade_events (trade_id, actor_id, event_type, from_status, to_status, detail)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            trade_id, actor_id, event_type, from_status, to_status,
            json.dumps(detail) if detail is not None else None,
        )
