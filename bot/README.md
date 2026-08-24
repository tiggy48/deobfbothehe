# LTC Middleman/Escrow Discord Bot

A Discord bot that runs a full ticket-based LTC (Litecoin) middleman trade
flow: request → confirm → pick sender/receiver → deposit → detect payment
on-chain → release → payout address → send LTC → complete. Built to run on
**Litecoin mainnet**, with an explicit configuration guard before real funds
can be used.

## Architecture

```
Discord bot (discord.py)  <->  PostgreSQL (asyncpg)  <->  BlockCypher LTC API  <->  Escrow state machine  <->  Payout
```

- **`main.py`** — process entrypoint. Boots the Discord bot, the aiohttp
  webhook server (for BlockCypher payment notifications), the deposit
  monitor's polling loop, and the trade-expiration loop.
- **`db/database.py`** — all trade-state-changing DB operations. Every
  operation takes a row lock (`SELECT ... FOR UPDATE`) and writes to
  `trade_events` for auditing. Nothing outside this file writes to the
  `trades` table.
- **`ltc/blockcypher_client.py`** — the *only* file that talks to the LTC
  blockchain. Knows nothing about Discord or Postgres, so it can be swapped
  for a different provider or a self-hosted `litecoind` node later without
  touching bot logic.
- **`ltc/monitor.py`** — payment detection. Listens for BlockCypher
  webhooks and also polls as a fallback; both paths converge on the same
  idempotent DB calls.
- **`utils/state.py`** — the canonical trade state machine definition.
- **`cogs/trade_flow.py`** — all user-facing ticket/button/modal flow.
- **`cogs/staff.py`** — staff slash commands (`/trade view`, `/trade
  cancel`, etc).

### Why BlockCypher

BlockCypher was chosen because it natively supports Litecoin **testnet**
(`test3`) and mainnet, gives address generation + balance/tx lookups +
webhook push notifications in one API (so you don't need to run and
maintain your own `litecoind` node), and its transaction flow lets us
**sign transactions locally** (`/txs/new` → sign digests ourselves →
`/txs/send`) so the escrow's private key is never sent to BlockCypher for
signing. If you'd rather run your own node, only `ltc/blockcypher_client.py`
needs to be replaced — everything else (DB, Discord flow, state machine)
is unaffected.

### Where the escrow's private keys come from

For each trade, the bot asks BlockCypher to generate a **fresh keypair and
address** dedicated to that one trade's escrow deposit. This is *not* a
user's wallet — it's the service's own custodial address that the bot
needs control of in order to later forward funds to the receiver. That
capability is inherent to any middleman/escrow bot. The private key is
encrypted at rest (`WALLET_ENCRYPTION_KEY`, Fernet symmetric encryption)
immediately after generation and only decrypted in-memory at payout time.
**The bot never asks a user for their own seed phrase or private key.**

## Setup

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New
   Application**.
2. Under **Bot**, click **Add Bot**.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent**
   (required to find the other trade participant by username).
4. Under **OAuth2 → URL Generator**, select scopes `bot` and
   `applications.commands`, and permissions: Manage Channels, View
   Channels, Send Messages, Embed Links, Read Message History, Use Slash
   Commands. Use the generated URL to invite the bot to your server.

### 2. Add the bot token

Copy the bot token from the **Bot** page and put it in `.env` (copy
`.env.example` to `.env` first):

```
DISCORD_BOT_TOKEN=...
```

Also set `STAFF_ROLE_ID` to the role ID that should have staff powers
(right-click the role in Discord with Developer Mode enabled → Copy ID).

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Set environment variables

Fill in the rest of `.env`:

- `WALLET_ENCRYPTION_KEY` — generate with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `LTC_API_KEY` — a free BlockCypher token from
  <https://accounts.blockcypher.com/> (works without one at low rate
  limits too, but a token is recommended).
- This deployment uses `LTC_NETWORK=main`; keep the mainnet confirmation flag
  enabled only if you understand that real LTC will move.

### 5. Creating the Railway PostgreSQL database

1. In your Railway project, click **New → Database → Add PostgreSQL**.
2. Railway automatically creates a `DATABASE_URL` variable. Reference it in
   your bot service's variables as `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   (or copy the value directly).
3. The bot creates all tables automatically on startup (`schema.sql` is
   run idempotently) — no manual migration step needed.

### 6. Deploying to Railway

1. Push this project to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**.
3. Add a PostgreSQL database to the project (step 5).
4. In your bot service's **Variables** tab, add every variable from
   `.env.example` (Railway auto-supplies `PORT`, which the bot uses for
   the webhook server — don't override it).
5. Set **Start Command** to `python main.py` (Railway usually detects this
   automatically from `requirements.txt`; if not, set it under Settings →
   Deploy).
6. Once deployed, note your public URL (Settings → Networking → Generate
   Domain) and set `WEBHOOK_PUBLIC_BASE_URL` to it, e.g.
   `https://your-app.up.railway.app`. Redeploy after setting it so new
   deposit addresses register webhooks against the right URL. (If you skip
   this, the bot still works — it just relies entirely on the polling
   fallback instead of push notifications, which is slightly slower but
   fully functional.)

### 7. Setting up Litecoin MAINNET

Nothing to install — BlockCypher hosts the Litecoin infrastructure. Make sure
`.env` has `LTC_NETWORK=main` and
`ALLOW_MAINNET_I_HAVE_TESTED_THOROUGHLY=true`.

### 8. Getting testnet LTC

Use a Litecoin testnet faucet to get free test coins for trying out a
deposit, e.g.:

- <https://testnet-faucet.com/ltc-testnet/>
- <https://tltc.bitaps.com/>

(Faucet availability changes over time — search "litecoin testnet faucet"
if a link is down.)

### 9. Testing a complete trade

1. Invite the bot, run `/panelsteup` in a channel (staff only) to post
   the **Request LTC** panel.
2. Click **Request LTC**, enter the other participant's username and the two
   items. The bot opens the private ticket only after it finds that member.
3. Both users click **✅ Confirm** in the generated ticket.
4. The ticket creator enters the dollar amount; both users confirm it.
5. Pick who the sender is.
6. The bot generates a fresh testnet deposit address. Send testnet LTC to
   it from a testnet wallet funded by the faucet.
7. Watch the ticket update automatically once the transaction is seen and
   then confirmed (default: 2 confirmations, ~5 minutes on testnet).
8. The sender clicks **🔓 Release LTC** → confirms.
9. The receiver clicks **💰 Enter LTC Address**, enters a testnet payout
   address, confirms.
10. The bot signs and broadcasts the payout transaction and marks the
   trade **COMPLETED**.

Use `/trade view`, `/trade audit-log`, `/trade lock`, `/trade cancel`, and
`/trade dispute`, plus `/release id:<trade_id>` when the sender needs staff
assistance (staff role required), to inspect and manage trades.

## Going to mainnet

Do **not** flip `LTC_NETWORK=main` until you've run multiple full testnet
trades successfully, including at least one deliberately-broken one (wrong
address, cancelled release, disputed trade) to confirm staff tooling
works. When you're ready:

1. Set `LTC_NETWORK=main`.
2. Set `ALLOW_MAINNET_I_HAVE_TESTED_THOROUGHLY=true` (the bot refuses to
   start on mainnet without this, on purpose).
3. Get a proper BlockCypher API token with adequate rate limits for your
   expected volume.
4. Consider adding real monitoring/alerting around the webhook server and
   the `payout_locks` table, since real money is now moving through it.

## Security notes

- All Discord authorization checks (staff role, sender-only /
  receiver-only button access, ticket participant checks) are re-verified
  **server-side** on every interaction against the real Discord user ID —
  never inferred from button labels or cached embeds.
- Trade state transitions are enforced by whitelisting the exact source
  state(s) each operation is allowed to start from (see `db/database.py`
  and `utils/state.py`); illegal skips raise `ConcurrencyError`.
- Payouts are protected from double-execution by a dedicated
  `payout_locks` table (`try_acquire_payout_lock`) plus a `UNIQUE`
  constraint on `payouts.trade_id`.
- Deposits are matched to trades only by the unique per-trade deposit
  address BlockCypher confirms on-chain — user-submitted transaction IDs
  or screenshots are never trusted.
- If a payout fails mid-flight, the trade is deliberately **not**
  auto-retried (to avoid any risk of double-spend from an ambiguous
  failure) — it's left for staff to review via `/trade view` and
  `/trade audit-log`. Network/connection failures are recorded as
  `UNKNOWN_REVIEW`, because the provider may have accepted the transaction
  even when its response was lost.
- Payouts reserve `PAYOUT_FEE_RESERVE_SATOSHIS` (100,000 by default) from the
  escrow deposit for the Litecoin miner fee. The previous version attempted
  to send the entire deposit, which can be rejected as insufficient funds.
