"""Entry point. Run with: python bot.py"""

import asyncio
import os
import re
import sys
import time

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "auxly.log")

# Version check is passive on purpose: the bot only READS the latest version
# number from GitHub and tells the owner — it never downloads or runs code.
# Auto-update was considered and rejected (supply-chain risk).
REPO_URL = "https://github.com/AlejandroGA-GHUB/Auxly"
VERSION_URL = ("https://raw.githubusercontent.com/AlejandroGA-GHUB/Auxly/"
               "main/VERSION.txt")

# VERSION.txt: line 1 is the version; the rest is a don't-edit note for
# users, so only the first line is ever read (here and from GitHub).
try:
    with open(os.path.join(BASE_DIR, "VERSION.txt"), encoding="utf-8") as _v:
        VERSION = _v.readline().strip()
except OSError:
    VERSION = "unknown"

# Windowless launches (auxly_start.bat starts the bot with pythonw so the
# terminal can be closed) have no stdout/stderr — print() would crash on
# None. Send output to auxly.log instead so errors stay findable.
LOGGING_TO_FILE = sys.stdout is None or sys.stderr is None
if LOGGING_TO_FILE:
    _log = open(LOG_PATH, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

load_dotenv()

PREFIX = "a!"
START_TIME = time.monotonic()

intents = discord.Intents.default()
intents.message_content = True  # required for prefix commands

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} — prefix is '{PREFIX}'. Ready to play!")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument. Try `{PREFIX}help`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"That argument doesn't look right. Try `{PREFIX}help`.")
        return
    if isinstance(error, commands.NotOwner):
        await ctx.send("Only the bot owner can do that.")
        return
    raise error


@bot.hybrid_command(name="help", help="Show this help message.")
async def help_command(ctx: commands.Context):
    lines = [
        "✨ Every command also works as a **/slash command** — same names, "
        "same behavior.\n",
        f"`{PREFIX}play <link or search>` — Play a song, or queue it if one is "
        "already playing. Handles YouTube links, Spotify links, playlists, and "
        "attached audio files (mp3, wav, flac, …) — just attach the file to "
        f"the `{PREFIX}play` message. (Attached files work with `{PREFIX}play` "
        "only, not `/play`.)",
        f"`{PREFIX}playnext <link or search>` — Like play, but jumps the queue "
        "(plays right after the current song).",
        f"`{PREFIX}join` — Bring the bot into your voice channel without "
        "playing anything.",
        f"`{PREFIX}pause` / `{PREFIX}resume` — Pause / resume the current song.",
        f"`{PREFIX}skip` — Skip the current song (cancels any loop).",
        f"`{PREFIX}loop <n>` — Repeat the current song n more times; the queue "
        f"waits until the loops finish. `{PREFIX}loop 0` cancels.",
        f"`{PREFIX}queue` — Show the queue.",
        f"`{PREFIX}shuffle` — Shuffle the queue (playing song keeps playing).",
        f"`{PREFIX}move <n>` — Move queue slot n to the front (plays next).",
        f"`{PREFIX}remove <n> [n ...]` — Remove queue slots by number "
        f"(1 = next up); `{PREFIX}remove 2 5 9` removes all three.",
        f"`{PREFIX}removerange <x> <y>` — Remove queue slots x through y "
        f"(`{PREFIX}removerange 2 5` or `{PREFIX}removerange 2-5` both work).",
        f"`{PREFIX}clear` — Empty the queue (keeps the playing song).",
        f"`{PREFIX}nowplaying` — Show the current song and elapsed time.\n"
        "🎛️ Now-playing messages have **Pause/Resume · Loop · Skip** buttons.",
        f"`{PREFIX}history` — Show the last 10 songs played.",
        f"`{PREFIX}save <playlist>` — Save the currently playing song to one "
        f"of your playlists (see `{PREFIX}profilehelp`).",
        f"`{PREFIX}stop` — Stop everything and leave the voice channel.",
        f"`{PREFIX}profilehelp` — Profiles & saved playlists: build song "
        "collections you can queue anytime, on any server.",
    ]
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="\n".join(lines),
        color=0x5865F2,
    )
    embed.set_footer(text="Auto-disconnects after 1 hour of silence.")
    await ctx.send(embed=embed)


def _is_newer(remote: str, local: str) -> bool:
    """True if remote looks like a later release than local. Numeric
    dotted compare when both parse; anything odd falls back to plain
    inequality (better a false 'update available' than a silent miss)."""
    try:
        return (tuple(int(p) for p in remote.split("."))
                > tuple(int(p) for p in local.split(".")))
    except ValueError:
        return remote != local


async def _latest_version() -> str | None:
    """Fetch the released version from GitHub's VERSION.txt; None if
    unreachable. Read-only by design — updating is always a manual
    download. The first line must look like a dotted version number, so
    an error page can never be mistaken for a release."""
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(VERSION_URL) as resp:
                if resp.status != 200:
                    return None
                lines = (await resp.text()).splitlines()
                first = lines[0].strip() if lines else ""
                return first if re.fullmatch(r"\d+(\.\d+)*", first) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None


@bot.command(name="version", help="(Owner only) Show the bot version and "
                                  "whether a newer release is available.")
@commands.is_owner()
async def version_cmd(ctx: commands.Context):
    latest = await _latest_version()
    if latest is None:
        note = "couldn't reach GitHub to check for updates"
    elif _is_newer(latest, VERSION):
        note = f"⬆️ **v{latest}** is available — download it from <{REPO_URL}>"
    else:
        note = "up to date"
    await ctx.send(f"🎵 Auxly **v{VERSION}** — {note}.")


async def _startup_version_check():
    await bot.wait_until_ready()
    latest = await _latest_version()
    if latest and _is_newer(latest, VERSION):
        print(f"Update available: v{latest} (running v{VERSION}). "
              f"Download it from {REPO_URL} — Auxly never updates itself.")
    elif latest:
        print(f"Auxly v{VERSION} — up to date.")
    else:
        print(f"Auxly v{VERSION} — update check skipped (GitHub unreachable).")


@bot.command(name="devhelp", help="(Owner only) List owner commands.")
@commands.is_owner()
async def devhelp(ctx: commands.Context):
    lines = [
        f"`{PREFIX}shutdown` — Cleanly shut the bot down.",
        f"`{PREFIX}profile delete <name>` — Delete any profile and all its "
        "playlists (frees its stored files too).",
        f"`{PREFIX}grantfiles @user` — Let a user store audio files with "
        f"`{PREFIX}playlist addfile` (25 MB/file, 100 files per profile, "
        "saved in `audio_files/`).",
        f"`{PREFIX}revokefiles @user` — Stop new uploads; their existing "
        "files stay playable.",
        f"`{PREFIX}fileperms` — List everyone with file storage.",
        f"`{PREFIX}storage` — Disk usage: database size, stored audio files, "
        "and who they belong to.",
        f"`{PREFIX}status` — Health check: uptime, yt-dlp version, what's "
        "playing where.",
        f"`{PREFIX}log [n]` — Show the last n lines of `auxly.log` "
        "(default 20; the log is written on windowless launches).",
        f"`{PREFIX}version` — Bot version + whether a newer release is out "
        "(updates are always a manual download).",
    ]
    embed = discord.Embed(
        title="🛠️ Owner Commands",
        description="\n".join(lines),
        color=0x5865F2,
    )
    embed.set_footer(text="Prefix-only and hidden from a!help on purpose.")
    await ctx.send(embed=embed)


@bot.command(name="status", help="(Owner only) Health check: uptime, yt-dlp "
                                 "version, what's playing where.")
@commands.is_owner()
async def status(ctx: commands.Context):
    import yt_dlp  # already loaded by sources.py; just reading the version

    up = int(time.monotonic() - START_TIME)
    days, rem = divmod(up, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime = (f"{days}d {hours}h {minutes}m" if days
              else f"{hours}h {minutes}m")
    n = len(bot.guilds)
    lines = [
        f"⏱️ Up **{uptime}** · Auxly v{VERSION} · "
        f"yt-dlp {yt_dlp.version.__version__} · "
        f"{n} server{'s' if n != 1 else ''}"
    ]

    music = bot.get_cog("Music")
    active = []
    for player in (music.players.values() if music else []):
        vc = player.guild.voice_client
        if player.task.done() or vc is None or not vc.is_connected():
            continue
        queued = len(player.queue)
        if player.current is not None:
            state = "paused" if vc.is_paused() else "playing"
            line = (f"🔊 **{player.guild.name}** — {state} "
                    f"*{player.current.title}* in `{vc.channel.name}`, "
                    f"{queued} queued")
            if player.loops_left:
                line += f", loop ×{player.loops_left}"
        else:
            line = (f"🔊 **{player.guild.name}** — idle in "
                    f"`{vc.channel.name}`, {queued} queued")
        active.append(line)
    lines += active or ["🔇 Not in any voice channel."]

    profiles = await asyncio.to_thread(storage.list_profiles)
    perms = await asyncio.to_thread(storage.list_file_perms)
    lines.append(
        f"👤 {len(profiles)} profile{'s' if len(profiles) != 1 else ''} · "
        f"{len(perms)} user{'s' if len(perms) != 1 else ''} with file storage"
    )
    embed = discord.Embed(
        title="📡 Auxly Status",
        description="\n".join(lines),
        color=0x5865F2,
    )
    await ctx.send(embed=embed)


@bot.command(name="log", help="(Owner only) Show the last n lines of "
                              "auxly.log (default 20).")
@commands.is_owner()
async def show_log(ctx: commands.Context, lines: int = 20):
    lines = max(1, lines)

    def read_log() -> list[str]:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return f.readlines()

    try:
        content = await asyncio.to_thread(read_log)
    except OSError:
        await ctx.send(
            "There's no `auxly.log` — it's written when the bot is launched "
            "windowless (`auxly_start.bat`); console runs print to the "
            "console instead."
        )
        return
    if not content:
        await ctx.send("`auxly.log` is empty — nothing logged this run.")
        return
    tail = content[-lines:]
    # Fit Discord's 2,000-char message cap (code fences + header included).
    while len(tail) > 1 and sum(len(l) for l in tail) > 1850:
        tail.pop(0)
    header = f"Last {len(tail)} of {len(content)} log lines"
    if not LOGGING_TO_FILE:
        header += " (⚠️ console run — this file is from a previous launch)"
    body = "".join(tail).replace("`", "'")
    if len(body) > 1850:  # one giant line (e.g. a traceback repr)
        body = body[-1850:]
    await ctx.send(f"{header}:\n```text\n{body}\n```")


@bot.command(name="shutdown", help="(Owner only) Cleanly shut the bot down.")
@commands.is_owner()
async def shutdown(ctx: commands.Context):
    await ctx.send("👋 Shutting down. Bye!")
    for vc in list(bot.voice_clients):
        await vc.disconnect(force=True)
    await bot.close()


async def setup_hook():
    await bot.load_extension("music")
    await bot.load_extension("profiles")
    asyncio.create_task(_startup_version_check())
    try:
        synced = await bot.tree.sync()  # register the /slash versions
        print(f"Synced {len(synced)} slash commands.")
    except discord.HTTPException as e:
        print(f"Slash command sync failed (prefix commands unaffected): {e}")


bot.setup_hook = setup_hook


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and paste "
            "your bot token in."
        )
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
