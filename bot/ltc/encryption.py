"""
Encrypts/decrypts the escrow's own per-trade wallet private keys at rest.

Important distinction: this is NOT for user seed phrases or user private
keys -- the bot never asks users for those. This encrypts the private key
of the deposit address that the ESCROW SERVICE ITSELF generates for each
trade, which the service needs in order to eventually forward the funds to
the receiver. That capability is inherent to any custodial middleman bot.

The key used here (WALLET_ENCRYPTION_KEY) must be a Fernet key, generated
once with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

and stored only as an environment variable / secret manager entry -- never
committed to source control.
"""

from cryptography.fernet import Fernet, InvalidToken


class WalletEncryption:
    def __init__(self, fernet_key: str):
        try:
            self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
        except Exception as e:
            raise ValueError(
                "WALLET_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from e

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise ValueError("Failed to decrypt wallet private key -- wrong WALLET_ENCRYPTION_KEY?") from e
