"""
Central configuration module.

Every secret / environment-specific value is read from the environment.
Nothing here is hardcoded. If a required variable is missing, the bot
fails fast at startup with a clear error instead of limping along with
None values that would cause confusing failures later.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"[config] Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def _optional(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] Environment variable {name} must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


@dataclass(frozen=True)
class Config:
    # Discord
    discord_bot_token: str
    staff_role_id: int
    guild_id: Optional[int]

    # Database
    database_url: str

    # Litecoin / BlockCypher
    ltc_network: str            # "testnet" or "main"
    ltc_api_token: Optional[str]
    blockcypher_base_url: str = field(init=False)

    # Confirmations required before a deposit is considered final
    confirmations_required: int = 2

    # Encryption key used to encrypt per-trade wallet private keys at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    wallet_encryption_key: str = ""

    # Webhook server (for BlockCypher payment notifications)
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_public_base_url: Optional[str] = None  # e.g. https://your-app.up.railway.app

    # Trade behaviour
    trade_expiration_minutes: int = 60
    poll_interval_seconds: int = 30
    # Reserved from each escrow deposit to pay the network miner fee. The
    # previous implementation attempted to send 100% of the deposit, which
    # BlockCypher rejects because a transaction also needs an input fee.
    payout_fee_reserve_satoshis: int = 100_000

    def __post_init__(self):
        object.__setattr__(
            self,
            "blockcypher_base_url",
            f"https://api.blockcypher.com/v1/ltc/{'test3' if self.ltc_network == 'testnet' else 'main'}",
        )


def load_config() -> Config:
    ltc_network = _optional("LTC_NETWORK", "main")
    if ltc_network not in ("testnet", "main"):
        print("[config] LTC_NETWORK must be 'testnet' or 'main'", file=sys.stderr)
        sys.exit(1)

    if ltc_network == "main":
        # Extra-loud guard rail: this project is meant to be proven out on
        # testnet before any real-money path is enabled.
        allow_mainnet = _optional("ALLOW_MAINNET_I_HAVE_TESTED_THOROUGHLY", "false").lower() == "true"
        if not allow_mainnet:
            print(
                "[config] Refusing to start with LTC_NETWORK=main. "
                "Finish testing on testnet first. If you have, set "
                "ALLOW_MAINNET_I_HAVE_TESTED_THOROUGHLY=true to proceed.",
                file=sys.stderr,
            )
            sys.exit(1)

    payout_fee_reserve_satoshis = _int("PAYOUT_FEE_RESERVE_SATOSHIS", 100_000)
    if payout_fee_reserve_satoshis < 0:
        print("[config] PAYOUT_FEE_RESERVE_SATOSHIS cannot be negative", file=sys.stderr)
        sys.exit(1)

    wallet_key = _require("WALLET_ENCRYPTION_KEY")

    guild_id_raw = _optional("GUILD_ID")

    cfg = Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        staff_role_id=int(_require("STAFF_ROLE_ID")),
        guild_id=int(guild_id_raw) if guild_id_raw else None,
        database_url=_require("DATABASE_URL"),
        ltc_network=ltc_network,
        ltc_api_token=_optional("LTC_API_KEY"),
        confirmations_required=_int("CONFIRMATIONS_REQUIRED", 2),
        wallet_encryption_key=wallet_key,
        webhook_host=_optional("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_int("PORT", _int("WEBHOOK_PORT", 8080)),  # Railway injects PORT
        webhook_public_base_url=_optional("WEBHOOK_PUBLIC_BASE_URL"),
        trade_expiration_minutes=_int("TRADE_EXPIRATION_MINUTES", 60),
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 30),
        payout_fee_reserve_satoshis=payout_fee_reserve_satoshis,
    )
    return cfg


CONFIG = load_config()
