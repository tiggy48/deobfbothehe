"""
Payment detection.

Primary path: BlockCypher webhook (`tx-confirmation` event) posts to our
aiohttp callback server the moment a transaction touching a deposit
address is seen / gains a confirmation.

Fallback path: a periodic polling loop walks every trade currently
AWAITING_DEPOSIT or DEPOSIT_DETECTED and asks BlockCypher for the address's
current status directly. This covers webhook delivery failures, missed
registrations, or Railway restarts wiping in-memory webhook state.

Both paths converge on the same `_process_observation` method, and the
database layer (`record_deposit_seen` / `confirm_deposit`) is written to be
idempotent, so it does not matter which path (or both) triggers first.

We never trust a user-submitted screenshot or user-submitted transaction
ID -- the only data plane that can advance a trade's deposit status is
this module talking directly to the BlockCypher API.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from db.database import Database
from ltc.blockcypher_client import BlockCypherClient, satoshis_to_ltc

logger = logging.getLogger("ltc.monitor")

NotifyCallback = Callable[[int, str, dict], Awaitable[None]]


class DepositMonitor:
    def __init__(self, db: Database, make_client: Callable[[], BlockCypherClient],
                 confirmations_required: int, poll_interval_seconds: int,
                 notify: Optional[NotifyCallback] = None):
        self._db = db
        self._make_client = make_client
        self._confirmations_required = confirmations_required
        self._poll_interval_seconds = poll_interval_seconds
        self._notify = notify
        self._poll_task: Optional[asyncio.Task] = None
        self._stopping = False

    def set_notify_callback(self, notify: NotifyCallback) -> None:
        self._notify = notify

    # ------------------------------------------------------------------
    # Webhook path
    # ------------------------------------------------------------------

    async def handle_webhook_event(self, payload: dict) -> None:
        """Handle a BlockCypher `tx-confirmation` webhook payload.

        Expected shape (BlockCypher tx-confirmation event):
        {
          "hash": "...", "confirmations": 1,
          "addresses": ["<deposit address>"],
          "outputs": [{"addresses": [...], "value": 12345}, ...],
          ...
        }
        """
        tx_id = payload.get("hash")
        confirmations = payload.get("confirmations", 0)
        addresses = payload.get("addresses") or []
        outputs = payload.get("outputs") or []

        if not tx_id or not addresses:
            logger.warning("Ignoring malformed webhook payload: %s", payload)
            return

        for address in addresses:
            trade = await self._db.get_trade_by_deposit_address(address)
            if not trade:
                continue

            amount_satoshis = 0
            for out in outputs:
                if address in (out.get("addresses") or []):
                    amount_satoshis += out.get("value", 0)

            await self._process_observation(
                trade_id=trade["trade_id"],
                address=address,
                tx_id=tx_id,
                amount_satoshis=amount_satoshis,
                confirmations=confirmations,
            )

    # ------------------------------------------------------------------
    # Polling fallback path
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._poll_task is None:
            self._stopping = False
            self._poll_task = asyncio.create_task(self._poll_loop(), name="ltc-deposit-poll-loop")

    async def stop(self) -> None:
        self._stopping = True
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during deposit polling cycle")
            await asyncio.sleep(self._poll_interval_seconds)

    async def _poll_once(self) -> None:
        trades = await self._db.list_awaiting_deposit_trades()
        if not trades:
            return
        async with self._make_client() as client:
            for trade in trades:
                address = trade.get("deposit_address")
                if not address:
                    continue
                try:
                    status = await client.get_address_status(address)
                except Exception:
                    logger.exception("Failed to poll address %s for trade %s", address, trade["trade_id"])
                    continue

                if status.latest_tx_id:
                    await self._process_observation(
                        trade_id=trade["trade_id"],
                        address=address,
                        tx_id=status.latest_tx_id,
                        amount_satoshis=status.latest_tx_amount_satoshis,
                        confirmations=status.latest_tx_confirmations,
                    )

    # ------------------------------------------------------------------
    # Shared handling
    # ------------------------------------------------------------------

    async def _process_observation(self, trade_id: int, address: str, tx_id: str,
                                     amount_satoshis: int, confirmations: int) -> None:
        amount_ltc: Decimal = satoshis_to_ltc(amount_satoshis) if amount_satoshis else Decimal("0")

        trade_before = await self._db.get_trade(trade_id)
        if trade_before is None:
            return
        was_detected_already = trade_before["status"] != "AWAITING_DEPOSIT"

        trade = await self._db.record_deposit_seen(
            trade_id=trade_id, tx_id=tx_id, address=address,
            amount=amount_ltc, confirmations=confirmations,
        )

        if not was_detected_already and trade["status"] == "DEPOSIT_DETECTED" and self._notify:
            await self._notify(trade_id, "deposit_seen", {
                "tx_id": tx_id, "amount": str(amount_ltc), "confirmations": confirmations,
                "confirmations_required": self._confirmations_required,
            })

        if confirmations >= self._confirmations_required and trade["status"] == "DEPOSIT_DETECTED":
            confirmed_trade = await self._db.confirm_deposit(trade_id)
            if confirmed_trade["status"] == "LTC_CONFIRMED" and self._notify:
                await self._notify(trade_id, "deposit_confirmed", {
                    "tx_id": tx_id, "amount": str(amount_ltc),
                })
        elif self._notify and was_detected_already and trade["status"] == "DEPOSIT_DETECTED":
            # Confirmation count ticked up but not yet enough -- send a lightweight progress update.
            await self._notify(trade_id, "deposit_progress", {
                "tx_id": tx_id, "amount": str(amount_ltc), "confirmations": confirmations,
                "confirmations_required": self._confirmations_required,
            })
