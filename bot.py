import asyncio
import io
import json
import os
import re
import time
import pathlib
import subprocess
from datetime import datetime

import aiohttp
import discord

# ------------------------------------------------------------------ config
TOKEN      = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = 1541381741577510912
PREFIX     = ".l"
TIMEOUT    = 100            # seconds per script before it gets skipped
LV2_PREFIX = ".lv2"
LV2_SCRIPT = "main_v2.luau"  # alternate/newer  engine, separate from main.luau
MAX_DL     = 8 * 1024 * 1024  # max bytes to pull from a raw link

# ------------------------------------------------------------------ .get command config
GET_PREFIX     = ".get"
GET_COOLDOWN   = 30           # seconds between uses, per user
BLACKLISTED_SITES: list[str] = [
    # fill this in (e.g. "example-bad-site.com") - 
    # badSites.json, move that list here
]
_get_cooldowns: dict[int, float] = {}
_own_ips: set[str] = set()

# ------------------------------------------------------------------ .upload command config
UPLOAD_PREFIX = ".upload"
PASTEFY_API   = "https://pastefy.app/api/v2/paste"

# ------------------------------------------------------------------ .promdeobf command config
PROMDEOBF_PREFIX  = ".promdeobf"
NODE_BIN          = "node"   # Node.js must be on PATH
PROMDEOBF_TIMEOUT = 60

# ------------------------------------------------------------------ .lv2 command config
LV2_PREFIX  = ".lv2"
LV2_TIMEOUT = 100

# ------------------------------------------------------------------ .obf command config (Prometheus)
OBF_PREFIX = ".obf"
OBF_DESCRIPTIONS = {
    "anti-tamper": "Enables our anti-tamper (breaks most env loggers / sandboxed VMs, only runs on Roblox)",
    "encrypt-strings": "Enables string encryption (hides your strings from prints etc.)",
}
_obf_sessions: dict[int, "ObfSession"] = {}

# ------------------------------------------------------------------ .moonveil command config
MOONVEIL_PREFIX   = ".moonveil"
MOONVEIL_COOLDOWN = 24 * 60 * 60  # one free use per user per day
MOONVEIL_API_URL  = "https://moonveil.cc/api/obfuscate"
# copied as-is from the original bot.py - if moonveil.cc rejects it,
# you'll need to grab a fresh key from your moonveil.cc account
MOONVEIL_BEARER   = os.environ.get("MOONVEIL_BEARER", "")
MOONVEIL_OPTIONS  = {
    "cffDecomposeExpr": False,
    "cffEnable": True,
    "cffHoistLocals": True,
    "cffWrapBlocks": True,
    "mangleEnable": True,
    "mangleGlobals": True,
    "mangleNamedIndex": True,
    "mangleNumbers": False,
    "mangleSelfCalls": True,
    "mangleStrings": True,
    "prettify": False,
    "removeCompoundAssign": True,
    "removeIfExpr": True,
    "vmEnable": True,
    "vmWrapScript": True,
}

# ------------------------------------------------------------------ .beautify command config
BEAUTIFY_PREFIX  = ".beautify"
BEAUTIFY_TIMEOUT = 30

# ------------------------------------------------------------------ paths
IS_WINDOWS = os.name == "nt"

ROOT = pathlib.Path(__file__).resolve().parent
LUTE = ROOT / ("lute.exe" if IS_WINDOWS else "lute")
LUNE_BIN = "lune.exe" if IS_WINDOWS else "lune"
TMP  = ROOT / "bot_tmp"
TMP.mkdir(exist_ok=True)

MOONVEIL_DATA_FILE = ROOT / "moonveil_data.json"

# PrometheusObf is referenced  too, but its source files
# weren't in either zip - download it and put it in this folder as
# PrometheusObf/ : https://github.com/prometheus-lua/Prometheus
PROMETHEUS_CLI = ROOT / "PrometheusObf" / "cli.lua"

PROMDEOBF_DIR  = ROOT / "promdeobf"
PROMDEOBF_MAIN = PROMDEOBF_DIR / "main.js"

MAIN_V2 = ROOT / "main_v2.luau"

# Prefer the lua(.exe) bundled right next to bot.py - relying on "lua" alone
# and hoping Windows searches the cwd for it is unreliable. Falls back to
# whatever "lua"/"lua.exe" resolves to on PATH if nothing's bundled.
_bundled_lua = ROOT / ("lua.exe" if IS_WINDOWS else "lua")
LUA_BIN = str(_bundled_lua) if _bundled_lua.exists() else ("lua.exe" if IS_WINDOWS else "lua")

BEAUTIFY_DIR    = ROOT / "beautifier"
BEAUTIFY_CLI    = BEAUTIFY_DIR / "cli.js"

BEAUTIFIER_DIR = ROOT / "beautifier"
BEAUTIFIER_CLI = BEAUTIFIER_DIR / "cli.js"

ACCENT  = 0x5865F2
GOOD    = 0x57F287
BAD     = 0xED4245
WARN    = 0xFEE75C

URL_RE  = re.compile(r"https?://[^\s<>()]+", re.I)
TIME_RE = re.compile(r"Finished processing in ([\d.]+) seconds", re.I)
OK_EXT  = (".lua", ".txt")

# ------------------------------------------------------------------ engine
def _kill_tree(pid: int):
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

def _dump_blocking(in_rel: str, out_rel: str, script: str = "main.luau"):
    """Run the exact same pipeline as the CLI. Returns (ok, reason, took)."""
    env = os.environ.copy()
    env["HOOKOP_BIN"] = str(LUTE)

    started = time.perf_counter()
    proc = subprocess.Popen(
        [LUNE_BIN, "run", script, in_rel, f"out={out_rel}"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0,
        start_new_session=not IS_WINDOWS,
    )
    try:
        log, _ = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try: proc.communicate(timeout=5)
        except Exception: pass
        return False, "timeout", TIMEOUT

    took = time.perf_counter() - started
    m = TIME_RE.search(log or "")
    if m:
        took = float(m.group(1))

    out_path = ROOT / out_rel
    if proc.returncode != 0 or not out_path.exists():
        tail = (log or "").strip().splitlines()[-1:] or ["unknown error"]
        return False, tail[-1][:300], took

    head = out_path.read_text(errors="ignore")[:6]
    if head.startswith("--err"):
        reason = out_path.read_text(errors="ignore")[5:].strip()
        return False, reason[:300] or "engine error", took

    return True, None, took

# ------------------------------------------------------------------ bot
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
queue: "asyncio.Queue[dict]" = asyncio.Queue()
http: aiohttp.ClientSession | None = None


async def react(msg, emoji):
    try: await msg.add_reaction(emoji)
    except discord.HTTPException: pass

async def unreact(msg, emoji):
    try: await msg.remove_reaction(emoji, bot.user)
    except discord.HTTPException: pass


async def gather_jobs(message) -> list[dict]:
    """Pull every dumpable script out of a message (and the one it replies to)."""
    sources = [message]
    if message.reference and message.reference.resolved:
        sources.append(message.reference.resolved)

    jobs, seen = [], set()
    for src in sources:
        for att in getattr(src, "attachments", []):
            if att.filename.lower().endswith(OK_EXT) and att.id not in seen:
                seen.add(att.id)
                jobs.append({"name": att.filename, "att": att, "url": None})

        text = getattr(src, "content", "") or ""
        for url in URL_RE.findall(text):
            url = url.rstrip(".,)`'\"")
            if url == PREFIX or url in seen:
                continue
            seen.add(url)
            name = url.split("?")[0].rstrip("/").split("/")[-1] or "script"
            if not name.lower().endswith(OK_EXT):
                name += ".lua"
            jobs.append({"name": name, "att": None, "url": url})
    return jobs


async def fetch_source(job) -> str:
    if job["att"] is not None:
        return (await job["att"].read()).decode("utf-8", "ignore")
    async with http.get(job["url"], timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        chunks, total = [], 0
        async for part in r.content.iter_chunked(65536):
            total += len(part)
            if total > MAX_DL:
                raise ValueError("file too large")
            chunks.append(part)
        return b"".join(chunks).decode("utf-8", "ignore")


async def worker():
    await bot.wait_until_ready()
    while True:
        job = await queue.get()
        message, name = job["message"], job["name"]
        stamp = f"{int(time.time()*1000)}_{os.getpid()}"
        in_rel  = f"bot_tmp/{stamp}.lua"
        out_rel = f"bot_tmp/{stamp}_out.lua"
        in_path, out_path = ROOT / in_rel, ROOT / out_rel

        await unreact(message, "🕓")
        await react(message, "⏳")
        try:
            src = await fetch_source(job)
            in_path.write_text(src, encoding="utf-8", errors="ignore")

            ok, reason, took = await asyncio.to_thread(_dump_blocking, in_rel, out_rel)

            if ok:
                data = out_path.read_text(errors="ignore")
                lines = data.count("\n") + 1
                e = discord.Embed(color=GOOD, timestamp=datetime.now())
                e.description = (
                    f"**`{name}`**\n"
                    f"`{lines:,} lines` · `{len(data)/1024:.1f} KB` · `{took:.2f}s`"
                )
                e.set_footer(text="Fire Hub")
                out_name = re.sub(r"\.(lua|txt)$", "", name, flags=re.I) + ".dump.lua"
                with open(out_path, "rb") as fh:
                    await message.reply(
                        content=message.author.mention,
                        embed=e,
                        file=discord.File(fh, filename=out_name),
                        mention_author=True,
                    )
                await unreact(message, "⏳")
                await react(message, "✅")
            else:
                label = "skipped — took over 100s" if reason == "timeout" else reason
                e = discord.Embed(color=WARN if reason == "timeout" else BAD,
                                  timestamp=datetime.now())
                e.description = f"**`{name}`**\n{label}"
                e.set_footer(text="Fire Hub")
                await message.reply(content=message.author.mention, embed=e,
                                    mention_author=True)
                await unreact(message, "⏳")
                await react(message, "⏱️" if reason == "timeout" else "❌")

        except Exception as ex:
            e = discord.Embed(color=BAD, timestamp=datetime.now())
            e.description = f"**`{name}`**\ncouldn't grab that — {ex}"
            e.set_footer(text="Fire Hub")
            try:
                await message.reply(content=message.author.mention, embed=e,
                                    mention_author=True)
            except discord.HTTPException:
                pass
            await unreact(message, "⏳")
            await react(message, "❌")
        finally:
            for p in (in_path, out_path):
                try: p.unlink()
                except OSError: pass
            queue.task_done()


# ------------------------------------------------------------------ shared helper
async def extract_code(message: discord.Message, arg: str) -> str | None:
    """Pull lua/txt content from: an attachment on this message, pasted text
    after the command, or a message this one replies to (its attachment or
    its text)."""
    for att in message.attachments:
        if att.filename.lower().endswith(OK_EXT):
            return (await att.read()).decode("utf-8", "ignore")

    if arg.strip():
        return arg.strip()

    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        for att in getattr(ref, "attachments", []):
            if att.filename.lower().endswith(OK_EXT):
                return (await att.read()).decode("utf-8", "ignore")
        text = getattr(ref, "content", None)
        if text:
            return text

    return None


# ------------------------------------------------------------------ .lv2 command
async def handle_lv2(message: discord.Message, arg: str):
    script_path = ROOT / LV2_SCRIPT
    if not script_path.exists():
        await message.reply(
            f"lv2 engine isn't set up (`{LV2_SCRIPT}` missing from the bot folder).",
            mention_author=False)
        return

    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{LV2_PREFIX}`. This runs the alternate engine.",
            mention_author=False)
        return

    stamp = f"{int(time.time()*1000)}_{message.author.id}"
    in_rel  = f"bot_tmp/{stamp}_lv2.lua"
    out_rel = f"bot_tmp/{stamp}_lv2_out.lua"
    in_path, out_path = ROOT / in_rel, ROOT / out_rel
    in_path.write_text(content, encoding="utf-8", errors="ignore")

    try:
        ok, reason, took = await asyncio.to_thread(_dump_blocking, in_rel, out_rel, LV2_SCRIPT)

        if not ok:
            label = "timed out after 100s" if reason == "timeout" else reason
            await message.reply(f"lv2 failed: {label}", mention_author=False)
            return

        data = out_path.read_text(errors="ignore")
        buf = io.BytesIO(data.encode("utf-8", "ignore"))
        await message.reply(
            content=f"Dumped in {took:.2f}s (lv2 engine)",
            file=discord.File(buf, filename="lv2_dump.lua"),
            mention_author=False)
    except Exception as ex:
        await message.reply(f"Unexpected error: {type(ex).__name__}: {ex}", mention_author=False)
    finally:
        for p in (in_path, out_path):
            try: p.unlink()
            except OSError: pass


# ------------------------------------------------------------------ .get command
def _cooldown_remaining(user_id: int) -> float:
    last = _get_cooldowns.get(user_id, 0.0)
    remaining = last + GET_COOLDOWN - time.time()
    return remaining if remaining > 0 else 0.0


def _redact_own_ips(text: str) -> str:
    for ip in _own_ips:
        text = text.replace(ip, ":P")
    return text


async def _refresh_own_ip():
    """Same idea as Node's blockIps: learn our own public IP so it can be
    masked out of .get's output (so it doesn't leak the server's IP)."""
    try:
        async with http.get("https://ipinfo.io/json",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json(content_type=None)
            ip = data.get("ip")
            if ip:
                _own_ips.add(ip)
    except Exception:
        pass


async def handle_get(message: discord.Message, arg: str):
    try:
        await _handle_get_inner(message, arg)
    except Exception as ex:
        try:
            await message.reply(f"Unexpected error: {type(ex).__name__}: {ex}",
                                 mention_author=False)
        except discord.HTTPException:
            pass


async def _handle_get_inner(message: discord.Message, arg: str):
    remaining = _cooldown_remaining(message.author.id)
    if remaining > 0:
        await message.reply(
            f"You need to wait `{int(remaining)}s` before using this command again.",
            mention_author=False)
        return

    url = arg.strip().split()[0] if arg.strip() else ""
    if not url.lower().startswith("http"):
        await message.reply(f"Please give a url. e.g. `{GET_PREFIX} https://example.com`",
                             mention_author=False)
        return

    if any(site in url for site in BLACKLISTED_SITES):
        await message.reply("That site is blacklisted, please try something else.",
                             mention_author=False)
        return

    try:
        chunks, total = [], 0
        async with http.get(
            url,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as r:
            if r.status >= 400:
                await message.reply(f"Request failed: HTTP {r.status} {r.reason}",
                                     mention_author=False)
                return
            async for part in r.content.iter_chunked(65536):
                total += len(part)
                if total > MAX_DL:
                    raise ValueError("file too large")
                chunks.append(part)
        data = b"".join(chunks).decode("utf-8", "ignore")
    except asyncio.TimeoutError:
        await message.reply("Request timed out (site took longer than 30s to respond).",
                             mention_author=False)
        return
    except aiohttp.ClientConnectorError as ex:
        await message.reply(f"Couldn't connect to that host: {ex}", mention_author=False)
        return
    except Exception as ex:
        await message.reply(f"Request failed: {type(ex).__name__}: {ex}", mention_author=False)
        return

    if not data.strip():
        await message.reply("Got an empty response from that url.", mention_author=False)
        return

    _get_cooldowns[message.author.id] = time.time()
    safe_data = _redact_own_ips(data)

    buf = io.BytesIO(safe_data.encode("utf-8", "ignore"))
    await message.reply(file=discord.File(buf, filename="http.txt"),
                         mention_author=False)


# ------------------------------------------------------------------ .obf command (Prometheus)
def _build_prometheus_config(settings: dict) -> str:
    """Builds a Prometheus config file (same shape as the official 'Strong'
    preset) with AntiTamper/EncryptStrings included or left out based on
    the toggles. Passing --anti-tamper:t style flags to the real Prometheus
    CLI does nothing - it only understands --preset/--config/--out/--LuaU/
    --pretty/--nocolors, so the only way to actually control these steps is
    through a generated config file like this one."""
    steps = ['{ Name = "Vmify", Settings = {} },']
    if settings.get("encrypt-strings"):
        steps.append('{ Name = "EncryptStrings", Settings = {} },')
    if settings.get("anti-tamper"):
        steps.append('{ Name = "AntiTamper", Settings = { UseDebug = false } },')
    steps.append('{ Name = "Vmify", Settings = {} },')
    steps.append(
        '{ Name = "ConstantArray", Settings = { Threshold = 1, StringsOnly = true, '
        'Shuffle = true, Rotate = true, LocalWrapperThreshold = 0 } },'
    )
    steps.append('{ Name = "NumbersToExpressions", Settings = { NumberRepresentationMutation = true } },')
    steps.append('{ Name = "WrapInFunction", Settings = {} },')

    steps_src = "\n\t\t".join(steps)
    return (
        "return {\n"
        '\tLuaVersion = "Lua51",\n'
        '\tVarNamePrefix = "",\n'
        '\tNameGenerator = "MangledShuffled",\n'
        "\tPrettyPrint = false,\n"
        "\tSeed = 0,\n"
        "\tSteps = {\n"
        f"\t\t{steps_src}\n"
        "\t},\n"
        "}\n"
    )


class ObfSession:
    __slots__ = ("content", "settings")

    def __init__(self, content: str):
        self.content = content
        self.settings = {name: False for name in OBF_DESCRIPTIONS}


class ObfView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        sess = _obf_sessions[self.user_id]

        for name in OBF_DESCRIPTIONS:
            on = sess.settings[name]
            btn = discord.ui.Button(
                label=f"{name}: {'on' if on else 'off'}",
                style=discord.ButtonStyle.success if on else discord.ButtonStyle.danger,
            )
            btn.callback = self._make_toggle_cb(name)
            self.add_item(btn)

        run_btn = discord.ui.Button(label="Obfuscate!", style=discord.ButtonStyle.primary)
        run_btn.callback = self._run_cb
        self.add_item(run_btn)

    def _make_toggle_cb(self, name: str):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("stop touching me weirdo", ephemeral=True)
            sess = _obf_sessions.get(self.user_id)
            if sess is None:
                return await interaction.response.edit_message(content=f"Session expired, start again with `{OBF_PREFIX}`.", embed=None, view=None)
            sess.settings[name] = not sess.settings[name]
            self._build_buttons()
            await interaction.response.edit_message(view=self)
        return cb

    async def _run_cb(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("stop touching me weirdo", ephemeral=True)

        sess = _obf_sessions.pop(self.user_id, None)
        if sess is None:
            return await interaction.response.edit_message(content=f"Session expired, start again with `{OBF_PREFIX}`.", embed=None, view=None)

        self.stop()
        await interaction.response.edit_message(content="Obfuscating...", embed=None, view=None)

        if not PROMETHEUS_CLI.exists():
            await interaction.message.edit(
                content=(f"PrometheusObf not found: `{PROMETHEUS_CLI}`\n"
                         f"Download it from github.com/prometheus-lua/Prometheus and put it "
                         f"in this bot's folder as `PrometheusObf/`."))
            return

        stamp = f"{int(time.time()*1000)}_{interaction.user.id}"
        in_path     = TMP / f"{stamp}_obfin.txt"
        out_path    = TMP / f"{stamp}_obfout.txt"
        config_path = TMP / f"{stamp}_obfconfig.lua"

        in_path.write_text(sess.content, encoding="utf-8", errors="ignore")
        config_path.write_text(_build_prometheus_config(sess.settings), encoding="utf-8")

        settings_str = [f"{name}: {'on' if val else 'off'}" for name, val in sess.settings.items()]

        args = [LUA_BIN, str(PROMETHEUS_CLI), "--LuaU", "--config", str(config_path),
                "--out", str(out_path), str(in_path)]

        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(ROOT))
            _, stderr = await proc.communicate()
        except FileNotFoundError:
            await interaction.message.edit(
                content=f"`{LUA_BIN}` not found, please install a Lua interpreter and add it to PATH.")
            in_path.unlink(missing_ok=True)
            config_path.unlink(missing_ok=True)
            return

        took = int((time.perf_counter() - start) * 1000)

        if proc.returncode != 0 or not out_path.exists():
            reason = ("Unable to obfuscate, possibly a syntax error "
                      "(this obfuscator does not fully support luau syntax)"
                      if proc.returncode == 1 else
                      f"Unable to obfuscate, error code #{proc.returncode}\n"
                      f"```{(stderr or b'').decode(errors='ignore')[:500]}```")
            await interaction.message.edit(content=reason)
        else:
            with open(out_path, "rb") as fh:
                await interaction.message.edit(
                    content=f"Obfuscated in {took}ms. Settings: {', '.join(settings_str)}",
                    attachments=[discord.File(fh, filename="obfuscated.lua")])

        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)


async def handle_obf(message: discord.Message, arg: str):
    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{OBF_PREFIX}`.",
            mention_author=False)
        return

    _obf_sessions[message.author.id] = ObfSession(content)
    view = ObfView(message.author.id)

    desc = "\n".join(f"**{k}**\n> -# {v}" for k, v in OBF_DESCRIPTIONS.items())
    e = discord.Embed(title="Obfuscation Settings", description=desc, color=ACCENT)
    await message.reply(embed=e, view=view, mention_author=False)


# ------------------------------------------------------------------ .moonveil command
def _load_moonveil_data() -> dict:
    try:
        return json.loads(MOONVEIL_DATA_FILE.read_text())
    except Exception:
        return {}


def _save_moonveil_data(data: dict):
    try:
        MOONVEIL_DATA_FILE.write_text(json.dumps(data))
    except Exception:
        pass


_moonveil_data = _load_moonveil_data()


async def handle_moonveil(message: discord.Message, arg: str):
    uid = str(message.author.id)
    last = _moonveil_data.get(uid, 0)
    remaining = last + MOONVEIL_COOLDOWN - time.time()
    if remaining > 0:
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        await message.reply(
            f"You already used your free daily obfuscation, wait {mins}m {secs}s to use it again.",
            mention_author=False)
        return

    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{MOONVEIL_PREFIX}`.",
            mention_author=False)
        return

    try:
        async with http.post(
            MOONVEIL_API_URL,
            json={"options": MOONVEIL_OPTIONS, "script": content},
            headers={
                "Authorization": f"Bearer {MOONVEIL_BEARER}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            r.raise_for_status()
            result = await r.text()
    except Exception as ex:
        await message.reply(f"Moonveil request failed: {ex}", mention_author=False)
        return

    # only mark the daily use as spent once the request actually succeeded
    _moonveil_data[uid] = time.time()
    _save_moonveil_data(_moonveil_data)

    buf = io.BytesIO(result.encode("utf-8", "ignore"))
    await message.reply(file=discord.File(buf, filename="moonveil.lua"),
                         mention_author=False)


# ------------------------------------------------------------------ .lv2 command)
async def handle_lv2(message: discord.Message, arg: str):
    if not MAIN_V2.exists() or not (ROOT / "env").exists():
        await message.reply(
            f"lv2 isn't set up (`{MAIN_V2}` or the `env/` folder is missing).",
            mention_author=False)
        return

    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{LV2_PREFIX}`.",
            mention_author=False)
        return

    stamp = f"{int(time.time()*1000)}_{message.author.id}"
    in_rel  = f"bot_tmp/{stamp}_lv2in.lua"
    out_rel = f"bot_tmp/{stamp}_lv2out.lua"
    in_path, out_path = ROOT / in_rel, ROOT / out_rel
    in_path.write_text(content, encoding="utf-8", errors="ignore")

    import os as _os
    _lv2_env = _os.environ.copy()
    _lv2_env["HOOKOP_BIN"] = str(LUTE)

    start = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            LUNE_BIN, "run", "main_v2.luau", in_rel, f"out={out_rel}", "hookOp",
            cwd=str(ROOT),
            env=_lv2_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=not IS_WINDOWS,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=LV2_TIMEOUT)
        except asyncio.TimeoutError:
            _kill_tree(proc.pid)
            await message.reply("lv2 timed out.", mention_author=False)
            in_path.unlink(missing_ok=True)
            return
    except FileNotFoundError:
        await message.reply(
            f"`{LUNE_BIN}` not found, please install lune and add it to PATH.",
            mention_author=False)
        in_path.unlink(missing_ok=True)
        return

    took = time.perf_counter() - start

    if proc.returncode != 0 or not out_path.exists():
        tail = (stdout or b"").decode(errors="ignore").strip().splitlines()
        reason = tail[-1][:400] if tail else "unknown error"
        await message.reply(f"Unable to process (lv2 engine).\n```{reason}```",
                             mention_author=False)
    else:
        data = out_path.read_text(errors="ignore")
        if data[:5] == "--err":
            await message.reply(f"Unable to process (lv2 engine).\n```{data[5:][:400]}```",
                                 mention_author=False)
        else:
            with open(out_path, "rb") as fh:
                await message.reply(
                    content=f"Processed in {took:.2f}s (lv2 engine)",
                    file=discord.File(fh, filename="lv2_out.lua"),
                    mention_author=False)

    in_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


# ------------------------------------------------------------------ .promdeobf command
async def handle_promdeobf(message: discord.Message, arg: str):
    if not PROMDEOBF_MAIN.exists():
        await message.reply(
            f"promdeobf isn't set up (`{PROMDEOBF_MAIN}` is missing).",
            mention_author=False)
        return

    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{PROMDEOBF_PREFIX}`. This only works on Prometheus-obfuscated scripts.",
            mention_author=False)
        return

    stamp = f"{int(time.time()*1000)}_{message.author.id}"
    in_path  = TMP / f"{stamp}_pdin.lua"
    out_path = TMP / f"{stamp}_pdout.lua"
    in_path.write_text(content, encoding="utf-8", errors="ignore")

    start = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            NODE_BIN, str(PROMDEOBF_MAIN), str(in_path), str(out_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(PROMDEOBF_DIR))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROMDEOBF_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await message.reply("promdeobf timed out.", mention_author=False)
            in_path.unlink(missing_ok=True)
            return
    except FileNotFoundError:
        await message.reply(
            f"`{NODE_BIN}` not found, please install Node.js and add it to PATH.",
            mention_author=False)
        in_path.unlink(missing_ok=True)
        return

    took = time.perf_counter() - start

    if proc.returncode != 0 or not out_path.exists():
        tail = (stdout or b"").decode(errors="ignore").strip().splitlines()
        reason = tail[-1][:400] if tail else "unknown error"
        await message.reply(
            f"Unable to deobfuscate (likely not Prometheus, or an unsupported variant).\n"
            f"```{reason}```",
            mention_author=False)
    else:
        with open(out_path, "rb") as fh:
            await message.reply(
                content=f"Deobfuscated in {took:.2f}s",
                file=discord.File(fh, filename="deobfuscated.lua"),
                mention_author=False)

    in_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


# ------------------------------------------------------------------ .beautify command
async def handle_beautify(message: discord.Message, arg: str):
    if not BEAUTIFIER_CLI.exists():
        await message.reply(f"beautifier isn't set up (`{BEAUTIFIER_CLI}` is missing).",
                             mention_author=False)
        return

    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{BEAUTIFY_PREFIX}`.",
            mention_author=False)
        return

    stamp = f"{int(time.time()*1000)}_{message.author.id}"
    in_path  = TMP / f"{stamp}_beauin.lua"
    out_path = TMP / f"{stamp}_beauout.lua"
    in_path.write_text(content, encoding="utf-8", errors="ignore")

    try:
        proc = await asyncio.create_subprocess_exec(
            NODE_BIN, str(BEAUTIFIER_CLI), str(in_path), str(out_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(BEAUTIFIER_DIR))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BEAUTIFY_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await message.reply("beautify timed out.", mention_author=False)
            in_path.unlink(missing_ok=True)
            return
    except FileNotFoundError:
        await message.reply(f"`{NODE_BIN}` not found, please install Node.js and add it to PATH.",
                             mention_author=False)
        in_path.unlink(missing_ok=True)
        return

    if proc.returncode != 0 or not out_path.exists():
        tail = (stderr or b"").decode(errors="ignore").strip().splitlines()
        reason = tail[-1][:400] if tail else "unknown error (probably invalid lua syntax)"
        await message.reply(f"Unable to beautify.\n```{reason}```", mention_author=False)
    else:
        with open(out_path, "rb") as fh:
            await message.reply(file=discord.File(fh, filename="beautified.lua"),
                                 mention_author=False)

    in_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


# ------------------------------------------------------------------ .upload command
async def handle_upload(message: discord.Message, arg: str):
    content = await extract_code(message, arg)
    if not content:
        await message.reply(
            f"Attach a `.lua`/`.txt` file, paste code directly, or reply to a message "
            f"with `{UPLOAD_PREFIX}`.",
            mention_author=False)
        return

    try:
        async with http.post(
            PASTEFY_API,
            json={
                "content": content,
                "title": f"{int(time.time())}.lua",
                "encrypted": False,
                "visibility": "UNLISTED",
                "type": "PASTE",
                "ai": False,
                "tags": [],
            },
            headers={"content-type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json()
    except Exception as ex:
        await message.reply(f"Error while uploading: {ex}", mention_author=False)
        return

    if not data.get("success"):
        await message.reply("Unable to upload to pastefy.", mention_author=False)
        return

    url = (data.get("paste") or {}).get("raw_url")
    if not url or not url.startswith("https://pastefy.app/"):
        await message.reply("pastefy.app is being weird right now, try again later.",
                             mention_author=False)
        return

    await message.reply(f"→ {url}", mention_author=False)


# ------------------------------------------------------------------ .help command
HELP_PREFIX = ".help"

HELP_TEXT = (
    "**Commands**\n\n"
    f"`{PREFIX}` — attach a `.lua`/`.txt`, drop a raw link, or reply to a "
    "message with one, then send this on its own. Dumps/deobfuscates it "
    "with the (hookOp) pipeline.\n\n"
    f"`{LV2_PREFIX}` — same idea as `{PREFIX}` but runs an alternate/newer "
    f"engine (`main_v2.luau`), useful when `{PREFIX}` gives an "
    "incomplete result on a script.\n\n"
    f"`{GET_PREFIX} <url>` — sends a GET request to the given url and "
    "replies with the response body as a file (`http.txt`). "
    f"{GET_COOLDOWN}s cooldown per user.\n\n"
    f"`{UPLOAD_PREFIX}` — attach/paste/reply with a `.lua`/`.txt`, uploads "
    "it to pastefy.app and replies with the link.\n\n"
    f"`{PROMDEOBF_PREFIX}` — attach/paste/reply with a `.lua`/`.txt` that's "
    "obfuscated with **Prometheus specifically** (not other obfuscators), "
    "deobfuscates it (string decryption, control-flow unflattening, "
    "constant array decoding) and replies with `deobfuscated.lua`.\n\n"
    f"`{OBF_PREFIX}` — attach/paste/reply with a `.lua`/`.txt`, pick "
    "anti-tamper/encrypt-strings settings from the buttons, hit "
    "\"Obfuscate!\" to run it through Prometheus.\n\n"
    f"`{MOONVEIL_PREFIX}` — attach/paste/reply with a `.lua`/`.txt` for one "
    "free obfuscation per user per day via moonveil.cc.\n\n"
    f"`{BEAUTIFY_PREFIX}` — attach/paste/reply with a `.lua`/`.txt`, "
    "reformats/pretty-prints it and replies with `beautified.lua`.\n\n"
    f"`{HELP_PREFIX}` — shows this message."
)


async def handle_help(message: discord.Message):
    e = discord.Embed(title="Help", description=HELP_TEXT, color=ACCENT)
    e.set_footer(text="Fire Hub")
    await message.reply(embed=e, mention_author=False)


@bot.event
async def on_ready():
    global http
    if http is None:
        http = aiohttp.ClientSession()
    bot.loop.create_task(worker())
    bot.loop.create_task(_refresh_own_ip())
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name=f"{PREFIX} · dumps"))
    print(f"online as {bot.user} · channel {CHANNEL_ID}")


@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return

    content = message.content.strip()

    # -- new commands get added here, one at a time --
    if content.lower() == LV2_PREFIX or content.lower().startswith(LV2_PREFIX + " "):
        await handle_lv2(message, content[len(LV2_PREFIX):])
        return

    if content.lower() == GET_PREFIX or content.lower().startswith(GET_PREFIX + " "):
        await handle_get(message, content[len(GET_PREFIX):])
        return

    if content.lower() == UPLOAD_PREFIX or content.lower().startswith(UPLOAD_PREFIX + " "):
        await handle_upload(message, content[len(UPLOAD_PREFIX):])
        return

    if content.lower() == PROMDEOBF_PREFIX or content.lower().startswith(PROMDEOBF_PREFIX + " "):
        await handle_promdeobf(message, content[len(PROMDEOBF_PREFIX):])
        return

    if content.lower() == LV2_PREFIX or content.lower().startswith(LV2_PREFIX + " "):
        await handle_lv2(message, content[len(LV2_PREFIX):])
        return

    if content.lower() == OBF_PREFIX or content.lower().startswith(OBF_PREFIX + " "):
        await handle_obf(message, content[len(OBF_PREFIX):])
        return

    if content.lower() == MOONVEIL_PREFIX or content.lower().startswith(MOONVEIL_PREFIX + " "):
        await handle_moonveil(message, content[len(MOONVEIL_PREFIX):])
        return

    if content.lower() == BEAUTIFY_PREFIX or content.lower().startswith(BEAUTIFY_PREFIX + " "):
        await handle_beautify(message, content[len(BEAUTIFY_PREFIX):])
        return

    if content.lower() == HELP_PREFIX:
        await handle_help(message)
        return

    if not (content == PREFIX or content.lower().startswith(PREFIX + " ")
            or content.lower().startswith(PREFIX + "\n")):
        return

    jobs = await gather_jobs(message)
    if not jobs:
        e = discord.Embed(color=ACCENT, description=(
            f"attach a `.lua`/`.txt`, drop a raw link, or reply to one with `{PREFIX}`."
        ))
        e.set_footer(text="Fire Hub")
        await message.reply(embed=e, mention_author=False)
        return

    await react(message, "🕓")
    pos = queue.qsize()
    for j in jobs:
        j["message"] = message
        await queue.put(j)
    if pos or len(jobs) > 1:
        note = f"queued `{len(jobs)}` · `{pos}` ahead" if pos else f"queued `{len(jobs)}`"
        try: await message.reply(note, mention_author=False, delete_after=6)
        except discord.HTTPException: pass


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not configured")
    bot.run(TOKEN)
