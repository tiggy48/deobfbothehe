# Fire Hub Discord Bot

## Railway setup

1. Create a new Railway service from this folder or from the repository.
2. Set the service start command to:

   ```text
   python bot.py
   ```

   If Railway deploys from the repository root instead, use:

   ```text
   python bot/bot.py
   ```

3. Add the required variable:

   - `DISCORD_TOKEN` — your Discord bot token

4. Deploy the service as a worker. This bot does not need a web port.

The bot listens only in channel `1541381741577510912`.

The repository includes a Dockerfile that installs Python, Node.js, npm, and
the Linux Lune runtime automatically on Railway. You do not need to install
Node.js manually or add it to PATH on Railway.

## Discord configuration

Enable the **Message Content Intent** for the bot in the Discord Developer
Portal. The bot also needs permission to view the target channel, read message
history, add reactions, and send messages/files.

## Optional feature

The `.moonveil` command additionally needs:

- `MOONVEIL_BEARER`

The token is intentionally not included in this source package.

## Notes

The imported Lua/Luau executables are Windows binaries. They are included for
reference, but Linux hosts such as Railway need Linux-compatible replacements
for commands that invoke those engines. The bot itself can still connect and
serve commands that do not require those binaries.
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

Railway rebuild trigger.
