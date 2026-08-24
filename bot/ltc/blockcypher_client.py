"""
Litecoin blockchain integration via the BlockCypher API.

Why BlockCypher:
  - Supports Litecoin testnet ("test3") and mainnet natively, so we can
    build and prove out the whole flow without touching real funds.
  - Gives us address generation, balance/tx lookups, AND webhook push
    notifications (POST callback on new tx / confirmations) so we don't
    have to run and maintain our own litecoind node just to ship an
    escrow bot.
  - Has a documented "create -> sign locally -> send" transaction flow, so
    we are never required to hand a private key to a third party to move
    funds (see `send_payout` below).

This module knows nothing about Discord or Postgres -- it is intentionally
kept isolated so the blockchain backend can be swapped out later (e.g. for
a self-hosted litecoind + your own signing code) without touching the bot
or database code.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

import aiohttp
import base58
import ecdsa
from ecdsa.util import sigencode_der


# Litecoin address prefixes.
# Mainnet: legacy 'L' (0x30), deprecated-legacy '3'/'M' P2SH (0x32), bech32 'ltc1'
# Testnet:  legacy 'm'/'n' (0x6f), P2SH 'Q'/'2' (0x3a), bech32 'tltc1'
_MAINNET_B58_PREFIXES = ("L", "M")
_TESTNET_B58_PREFIXES = ("m", "n", "Q", "2")
_BECH32_PREFIXES = {"main": "ltc1", "testnet": "tltc1"}


class BlockCypherError(Exception):
    pass


class InsufficientFundsError(BlockCypherError):
    pass


@dataclass
class GeneratedAddress:
    address: str
    private_key_hex: str
    public_key_hex: str


@dataclass
class AddressStatus:
    address: str
    balance_satoshis: int
    unconfirmed_balance_satoshis: int
    latest_tx_id: Optional[str]
    latest_tx_confirmations: int
    latest_tx_amount_satoshis: int


class BlockCypherClient:
    def __init__(self, base_url: str, network: str, api_token: Optional[str] = None,
                 session: Optional[aiohttp.ClientSession] = None):
        self._base_url = base_url.rstrip("/")
        self._network = network  # "testnet" or "main"
        self._api_token = api_token
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "BlockCypherClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        if self._owns_session and self._session:
            await self._session.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _params(self, extra: Optional[dict] = None) -> dict:
        params = dict(extra or {})
        if self._api_token:
            params["token"] = self._api_token
        return params

    async def _session_or_raise(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise BlockCypherError("BlockCypherClient used outside of an active session; use 'async with'")
        return self._session

    # ------------------------------------------------------------------
    # Address generation & validation
    # ------------------------------------------------------------------

    async def generate_address(self) -> GeneratedAddress:
        """Ask BlockCypher to generate a fresh keypair + address.

        BlockCypher returns the private key to us once, at creation time,
        over TLS -- it does not retain it. We immediately encrypt it
        (see ltc/encryption.py) before it ever touches the database.
        """
        session = await self._session_or_raise()
        async with session.post(self._url("/addrs"), params=self._params()) as resp:
            data = await self._handle_response(resp)
        return GeneratedAddress(
            address=data["address"],
            private_key_hex=data["private"],
            public_key_hex=data["public"],
        )

    async def get_ltc_usd_rate(self) -> Decimal:
        """Read the current LTC/USD spot price used to convert a trade value."""
        session = await self._session_or_raise()
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "litecoin", "vs_currencies": "usd"},
        ) as resp:
            data = await self._handle_response(resp)
        try:
            rate = Decimal(str(data["litecoin"]["usd"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise BlockCypherError("Price provider returned an invalid LTC/USD rate") from exc
        if rate <= 0:
            raise BlockCypherError("Price provider returned a non-positive LTC/USD rate")
        return rate

    @staticmethod
    def validate_address_format(address: str, network: str) -> bool:
        """Local, offline structural validation (no network round-trip).
        This is a fast first check; treat as necessary but not sufficient --
        callers should also rely on the transaction actually broadcasting
        successfully."""
        if not address or not isinstance(address, str):
            return False

        prefixes = _BECH32_PREFIXES.get("testnet" if network == "testnet" else "main")
        if address.startswith(prefixes):
            return bool(re.fullmatch(r"[a-z0-9]{20,90}", address))

        try:
            decoded = base58.b58decode_check(address)
        except Exception:
            return False
        if len(decoded) < 1:
            return False

        first_char = address[0]
        if network == "testnet":
            return first_char in _TESTNET_B58_PREFIXES
        return first_char in _MAINNET_B58_PREFIXES

    async def address_exists_on_chain(self, address: str) -> bool:
        """Extra network-side sanity check: ask BlockCypher to parse it."""
        session = await self._session_or_raise()
        async with session.get(self._url(f"/addrs/{address}/balance"), params=self._params()) as resp:
            if resp.status == 404:
                return False
            await self._handle_response(resp)
            return True

    # ------------------------------------------------------------------
    # Balance / transaction polling (fallback path)
    # ------------------------------------------------------------------

    async def get_address_status(self, address: str) -> AddressStatus:
        session = await self._session_or_raise()
        params = self._params({"unspentOnly": "false", "includeConfirmations": "true", "limit": 1})
        async with session.get(self._url(f"/addrs/{address}"), params=params) as resp:
            data = await self._handle_response(resp)

        tx_refs = data.get("txrefs", []) + data.get("unconfirmed_txrefs", [])
        latest = tx_refs[0] if tx_refs else None
        return AddressStatus(
            address=address,
            balance_satoshis=data.get("balance", 0),
            unconfirmed_balance_satoshis=data.get("unconfirmed_balance", 0),
            latest_tx_id=latest.get("tx_hash") if latest else None,
            latest_tx_confirmations=latest.get("confirmations", 0) if latest else 0,
            latest_tx_amount_satoshis=latest.get("value", 0) if latest else 0,
        )

    # ------------------------------------------------------------------
    # Webhooks (push notifications -- primary detection path)
    # ------------------------------------------------------------------

    async def create_address_webhook(self, address: str, callback_url: str,
                                      confirmations: int = 0) -> str:
        """Registers a webhook that fires when a tx touching `address` reaches
        `confirmations`. Register once at 0 confirmations (to catch the initial
        detection) and rely on the polling loop to track confirmation count
        upward, OR register multiple hooks at different confirmation
        thresholds. We use the simple approach: one hook per address at
        confirmations=0, and confirmation-count-updates fall back to polling.
        Returns the webhook id (so it can be torn down later)."""
        session = await self._session_or_raise()
        payload = {
            "event": "tx-confirmation",
            "address": address,
            "url": callback_url,
            "confirmations": confirmations,
        }
        async with session.post(self._url("/hooks"), params=self._params(), json=payload) as resp:
            data = await self._handle_response(resp)
        return data["id"]

    async def delete_webhook(self, webhook_id: str) -> None:
        session = await self._session_or_raise()
        async with session.delete(self._url(f"/hooks/{webhook_id}"), params=self._params()) as resp:
            if resp.status not in (200, 204, 404):
                await self._handle_response(resp)

    # ------------------------------------------------------------------
    # Sending payouts -- built and signed LOCALLY, never hand our key to
    # BlockCypher for signing.
    # ------------------------------------------------------------------

    async def send_payout(self, from_address: str, private_key_hex: str,
                           to_address: str, amount_satoshis: int) -> str:
        """Builds a transaction via BlockCypher's /txs/new skeleton endpoint,
        signs the resulting digests locally with the escrow private key, and
        submits the signed transaction via /txs/send. The private key never
        leaves this process."""
        session = await self._session_or_raise()

        new_tx_payload = {
            "inputs": [{"addresses": [from_address]}],
            "outputs": [{"addresses": [to_address], "value": amount_satoshis}],
        }
        async with session.post(self._url("/txs/new"), params=self._params(), json=new_tx_payload) as resp:
            skeleton = await self._handle_response(resp)

        errors = skeleton.get("errors")
        if errors:
            joined = "; ".join(e.get("error", str(e)) for e in errors)
            if "insufficient" in joined.lower():
                raise InsufficientFundsError(joined)
            raise BlockCypherError(f"BlockCypher /txs/new returned errors: {joined}")

        tosign = skeleton.get("tosign", [])
        if not tosign:
            raise BlockCypherError("BlockCypher did not return digests to sign")

        signing_key = ecdsa.SigningKey.from_string(bytes.fromhex(private_key_hex), curve=ecdsa.SECP256k1)
        verifying_key = signing_key.get_verifying_key()
        pubkey_hex = ("04" + verifying_key.to_string().hex())

        signatures = []
        pubkeys = []
        for digest_hex in tosign:
            digest_bytes = bytes.fromhex(digest_hex)
            signature = signing_key.sign_digest_deterministic(
                digest_bytes, hashfunc=hashlib.sha256, sigencode=sigencode_der
            )
            signatures.append(signature.hex())
            pubkeys.append(pubkey_hex)

        skeleton["signatures"] = signatures
        skeleton["pubkeys"] = pubkeys

        async with session.post(self._url("/txs/send"), params=self._params(), json=skeleton) as resp:
            final = await self._handle_response(resp)

        final_errors = final.get("errors")
        if final_errors:
            joined = "; ".join(e.get("error", str(e)) for e in final_errors)
            raise BlockCypherError(f"BlockCypher /txs/send returned errors: {joined}")

        tx_hash = final.get("tx", {}).get("hash")
        if not tx_hash:
            raise BlockCypherError("BlockCypher did not return a transaction hash after send")
        return tx_hash

    # ------------------------------------------------------------------

    @staticmethod
    async def _handle_response(resp: aiohttp.ClientResponse) -> dict:
        text = await resp.text()
        if resp.status >= 400:
            raise BlockCypherError(f"BlockCypher API error {resp.status}: {text}")
        if not text:
            return {}
        import json as _json
        return _json.loads(text)


def satoshis_to_ltc(satoshis: int) -> Decimal:
    return (Decimal(satoshis) / Decimal(10**8)).quantize(Decimal("0.00000001"))


def ltc_to_satoshis(amount: Decimal) -> int:
    return int((amount * Decimal(10**8)).to_integral_value())
