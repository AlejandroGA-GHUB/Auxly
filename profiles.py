"""Profiles cog: user profiles + saved playlists.

Profiles are global (keyed by Discord user ID — see storage.py), so a
playlist made in one server is usable in any server the bot runs in.
Everyone can view and queue anyone's playlist; only the profile owner can
change its contents; deleting a whole profile is bot-owner-only.
"""

import asyncio
import contextlib
import io
import os
import random
import re
import uuid

import discord
from discord.ext import commands

import sources
import storage
from music import PAGE_SIZE, PageView
from sources import Track, TrackError

# Profile names are one word: they anchor parsing for every [profile] arg.
NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,32}")
# Playlist names may contain spaces (e.g. "Frieren Battle"); commands parse
# them by position (trailing numbers/flag) or by matching existing names.
PLAYLIST_NAME_RE = re.compile(r"[A-Za-z0-9_\- ]{1,32}")
# Reserved so the optional shuffle flag on a!playlist playall is never
# ambiguous.
RESERVED_NAMES = {"s"}

# Per-file size cap for a!playlist addfile (disk guard: worst case per profile
# is PROFILE_FILE_CAP × this).
MAX_FILE_MB = 25

# Size cap for a!playlist import attachments — a real export of a full
# 1,000-song profile is well under 200 KB.
MAX_IMPORT_KB = 512


def _norm(name: str) -> str:
    """Collapse whitespace so 'Frieren  Battle' == 'Frieren Battle'."""
    return " ".join(name.split())


async def _ack(ctx: commands.Context):
    """Slash invocations must be acknowledged within 3 s; resolution and
    voice connect can take longer. No-op for prefix commands."""
    if ctx.interaction is not None and not ctx.interaction.response.is_done():
        await ctx.defer()


def _progress(ctx: commands.Context):
    """Typing indicator for prefix invocations; slash contexts are already
    deferred (ctx.typing() would double-defer and raise)."""
    return ctx.typing() if ctx.interaction is None else contextlib.nullcontext()

EMBED_COLOR = 0x5865F2
HELP_HINT = "See `a!profilehelp` for all profile & playlist commands."


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _db(func, *args):
    """Run a storage call off the event loop (SQLite is blocking)."""
    return await asyncio.to_thread(func, *args)


def _delete_stored(paths: list[str]) -> None:
    """Best-effort disk cleanup for removed stored-file songs (run via
    asyncio.to_thread — disk I/O must stay off the event loop)."""
    for p in paths:
        with contextlib.suppress(OSError):
            os.remove(storage.stored_file_abs(p))


def _track_source(source: str, stored: bool) -> str:
    """Stored files are kept as relative paths in the DB; hand FFmpeg the
    absolute path so playback never depends on the working directory."""
    return storage.stored_file_abs(source) if stored else source


class _ConfirmView(discord.ui.View):
    """Author-only [Add anyway]/[Cancel] prompt for a!playlist import when
    the target name already exists. result: True / False / None (timeout).
    Buttons vanish once answered (same convention as Now Playing controls)."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.result: bool | None = None

    async def interaction_check(
            self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person importing can answer this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Add anyway", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        self.result = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        self.result = False
        await interaction.response.edit_message(
            content="Import cancelled — nothing was added.", view=None
        )
        self.stop()


class Profiles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- helpers -------------------------------------------------------------

    @staticmethod
    async def _check_name(ctx: commands.Context, name: str) -> bool:
        if name.lower() in RESERVED_NAMES:
            await ctx.send(
                f"**{name}** is reserved (it's the optional shuffle flag on "
                f"`a!playlist playall`) — pick another name."
            )
            return False
        if NAME_RE.fullmatch(name):
            return True
        await ctx.send(
            "Profile names must be a single word (letters, numbers, `_`, `-`), "
            "up to 32 characters."
        )
        return False

    @staticmethod
    async def _check_playlist_name(ctx: commands.Context, name: str) -> bool:
        if any(w in RESERVED_NAMES for w in name.lower().split()):
            await ctx.send(
                "Playlist names can't contain the word **s** (it's the "
                "optional shuffle flag on `a!playlist playall`) — pick "
                "another name."
            )
            return False
        if PLAYLIST_NAME_RE.fullmatch(name):
            return True
        await ctx.send(
            "Playlist names can use letters, numbers, spaces, `_` and `-`, "
            "up to 32 characters."
        )
        return False

    async def _match_playlist(
        self, user_id: int, tokens: list[str]
    ) -> tuple[str, list[str]] | None:
        """Longest prefix of tokens that names one of the user's playlists
        (case-insensitive). Returns (stored_name, leftover_tokens)."""
        names = {
            n.lower(): n
            for n, _ in await _db(storage.list_playlists, user_id)
        }
        for k in range(len(tokens), 0, -1):
            candidate = " ".join(tokens[:k]).lower()
            if candidate in names:
                return names[candidate], tokens[k:]
        return None

    async def _resolve_target(
        self, ctx: commands.Context, tokens: list[str]
    ) -> tuple[int, str, str | None] | None:
        """Work out whose (user_id, profile_name) and which playlist (or None
        for 'list their playlists') a view/play command refers to. The first
        token may be a profile name (profiles are one word); the rest — or
        everything — is a playlist name, which may contain spaces."""
        if tokens:
            prof = await _db(storage.find_profile, tokens[0])
            if prof:
                playlist = _norm(" ".join(tokens[1:])) or None
                return prof[0], prof[1], playlist
            own = await _db(storage.get_profile, ctx.author.id)
            if own is None:
                await ctx.send(f"No profile named **{tokens[0]}**.")
                return None
            return ctx.author.id, own, _norm(" ".join(tokens))
        own = await _db(storage.get_profile, ctx.author.id)
        if own is None:
            await ctx.send(
                "You don't have a profile yet — create one with "
                "`a!profile create <name>`."
            )
            return None
        return ctx.author.id, own, None

    async def _show_playlists(self, ctx: commands.Context, user_id: int,
                              profile_name: str):
        try:
            playlists = await _db(storage.list_playlists, user_id)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if not playlists:
            await ctx.send(f"**{profile_name}** has no playlists yet.")
            return
        lines = [
            f"`{i}.` **{name}** — {count} song{'s' if count != 1 else ''}"
            for i, (name, count) in enumerate(playlists, 1)
        ]
        await ctx.send(embed=discord.Embed(
            title=f"🎧 {profile_name}'s playlists",
            description="\n".join(lines),
            color=EMBED_COLOR,
        ).set_footer(text="a!playlist view <profile> <name> to see the songs."))

    async def _show_songs(self, ctx: commands.Context, user_id: int,
                          profile_name: str, playlist: str):
        try:
            songs = await _db(storage.get_songs, user_id, playlist)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if not songs:
            await ctx.send(f"**{profile_name} → {playlist}** is empty.")
            return

        def render(page: int) -> tuple[discord.Embed, int]:
            pages = max(1, (len(songs) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, pages - 1))
            start = page * PAGE_SIZE
            lines = [
                f"`{i}.` {'📁 ' if stored else ''}{title} "
                f"[{_fmt_duration(duration)}]"
                for i, (title, _, duration, _, stored) in enumerate(
                    songs[start:start + PAGE_SIZE], start + 1)
            ]
            embed = discord.Embed(
                title=f"🎧 {profile_name} → {playlist} ({len(songs)} songs)",
                description="\n".join(lines),
                color=EMBED_COLOR,
            )
            if pages > 1:
                embed.set_footer(text=f"Page {page + 1}/{pages}")
            return embed, pages

        await PageView(render).send(ctx)

    # -- profile commands ------------------------------------------------------

    @commands.hybrid_group(invoke_without_command=True, case_insensitive=True,
                           help="Profile commands — see a!profilehelp.")
    async def profile(self, ctx: commands.Context):
        name = await _db(storage.get_profile, ctx.author.id)
        if name:
            await ctx.send(f"Your profile: **{name}**. {HELP_HINT}")
        else:
            await ctx.send(
                f"You don't have a profile yet — `a!profile create <name>`. "
                f"{HELP_HINT}"
            )

    @profile.command(name="create", help="Create your profile.")
    async def profile_create(self, ctx: commands.Context, name: str):
        if not await self._check_name(ctx, name):
            return
        try:
            await _db(storage.create_profile, ctx.author.id, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        await ctx.send(
            f"✅ Profile **{name}** created. Make a playlist with "
            f"`a!playlist create <name>`."
        )

    @profile.command(name="rename", help="Rename your profile.")
    async def profile_rename(self, ctx: commands.Context, newname: str):
        if not await self._check_name(ctx, newname):
            return
        try:
            old = await _db(storage.rename_profile, ctx.author.id, newname)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        await ctx.send(f"✅ Renamed **{old}** → **{newname}**.")

    @profile.command(name="delete", with_app_command=False,
                     help="(Owner only) Delete a profile.")
    @commands.is_owner()
    async def profile_delete(self, ctx: commands.Context, name: str):
        try:
            deleted, files = await _db(storage.delete_profile, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if files:
            await asyncio.to_thread(_delete_stored, files)
        await ctx.send(f"🗑️ Profile **{deleted}** and all its playlists deleted.")

    @commands.hybrid_command(help="List all profiles.")
    async def profiles(self, ctx: commands.Context):
        rows = await _db(storage.list_profiles)
        if not rows:
            await ctx.send(
                "No profiles yet — be the first: `a!profile create <name>`."
            )
            return
        lines = [
            f"`{i}.` **{name}** — {count} playlist{'s' if count != 1 else ''}"
            for i, (name, count) in enumerate(rows, 1)
        ]
        await ctx.send(embed=discord.Embed(
            title="👥 Profiles",
            description="\n".join(lines),
            color=EMBED_COLOR,
        ).set_footer(text="a!playlist view <profile> to see someone's playlists."))

    # -- playlist commands -----------------------------------------------------

    @commands.hybrid_group(invoke_without_command=True, case_insensitive=True,
                           help="Playlist commands — see a!profilehelp.")
    async def playlist(self, ctx: commands.Context):
        await ctx.send(HELP_HINT)

    @playlist.command(name="create", help="Create a playlist on your profile. "
                                          "Names can contain spaces.")
    async def playlist_create(self, ctx: commands.Context, *, name: str):
        name = _norm(name)
        if not await self._check_playlist_name(ctx, name):
            return
        try:
            await _db(storage.create_playlist, ctx.author.id, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        await ctx.send(
            f"✅ Playlist **{name}** created. Add songs with "
            f"`a!playlist add {name} <song or link>`."
        )

    @playlist.command(name="add",
                      help="Add a song to your playlist — anything a!play "
                           "accepts except attached files.")
    async def playlist_add(self, ctx: commands.Context, *,
                           args: str | None = None):
        if ctx.message.attachments:
            await ctx.send(
                "⚠️ `add` can't store attached files (Discord attachment "
                "links expire after ~24h) — if you have file perms from the "
                "bot owner, use `a!playlist addfile <name>` instead."
            )
            if not args:
                return
        if not args:
            await ctx.send("Usage: `a!playlist add <playlist> <song or link>`.")
            return
        # The playlist must already exist, so match the input's start against
        # your playlist names (longest match wins) — no quotes needed even
        # for names with spaces.
        try:
            match = await self._match_playlist(ctx.author.id, args.split())
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if match is None:
            await ctx.send(
                "That doesn't start with one of your playlists — check "
                "`a!playlist view`."
            )
            return
        name, rest = match
        if not rest:
            await ctx.send(f"Give me a song: `a!playlist add {name} <song or link>`.")
            return
        query = " ".join(rest)
        await _ack(ctx)
        async with _progress(ctx):
            try:
                tracks = await sources.resolve(query, ctx.author.display_name)
            except TrackError as e:
                await ctx.send(f"⚠️ {e}")
                return
            try:
                added, dupes = await _db(
                    storage.add_songs, ctx.author.id, name,
                    [(t.title, t.source, t.duration, t.direct) for t in tracks],
                )
            except storage.StorageError as e:
                await ctx.send(f"⚠️ {e}")
                return
        if added == 0:
            await ctx.send(
                f"**{tracks[0].title}** is already in **{name}**." if dupes == 1
                else f"All {dupes} songs are already in **{name}**."
            )
            return
        capped = len(tracks) - added - dupes
        if added == 1 and len(tracks) == 1:
            msg = f"➕ Added **{tracks[0].title}** to **{name}**"
        else:
            msg = f"➕ Added **{added}** song{'s' if added != 1 else ''} to **{name}**"
        notes = []
        if dupes:
            notes.append(f"{dupes} already in it")
        if capped:
            notes.append(f"{capped} over the "
                         f"{storage.PROFILE_SONG_CAP:,}-song cap")
        if notes:
            msg += " (skipped " + ", ".join(notes) + ")"
        await ctx.send(msg + ".")

    @playlist.command(name="addfile", with_app_command=False,
                      help="Store attached audio files in your playlist "
                           "(needs file-storage permission from the bot owner).")
    async def playlist_addfile(self, ctx: commands.Context, *, name: str):
        if not (await _db(storage.has_file_perm, ctx.author.id)
                or await self.bot.is_owner(ctx.author)):
            await ctx.send(
                "⚠️ Storing files needs permission from the bot owner."
            )
            return
        atts = ctx.message.attachments
        if not atts:
            await ctx.send(
                "Attach the audio file(s) to the `a!playlist addfile "
                "<playlist>` message itself. (Prefix only — slash messages "
                "can't carry attachments.)"
            )
            return
        name = _norm(name)
        try:
            room = await _db(storage.file_room, ctx.author.id, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        # Dedupe by filename among the playlist's stored files (uploads get
        # unique disk names, so the filename is a stored file's identity).
        existing = {
            t.lower() for t, _, _, _, stored in
            await _db(storage.get_songs, ctx.author.id, name) if stored
        }
        notes = []
        good = []
        for att in atts:
            if not att.filename.lower().endswith(sources.AUDIO_EXTS):
                notes.append(f"**{att.filename}** isn't an audio file")
            elif att.size > MAX_FILE_MB * 1024 * 1024:
                notes.append(f"**{att.filename}** is over {MAX_FILE_MB} MB")
            elif att.filename.lower() in existing:
                notes.append(f"**{att.filename}** is already in **{name}**")
            else:
                existing.add(att.filename.lower())
                good.append(att)
        if len(good) > room:
            notes.append(
                f"skipped {len(good) - room} — only {room} stored-file "
                f"slot{'s' if room != 1 else ''} left on your profile"
            )
            good = good[:room]
        added = []
        async with _progress(ctx):
            for att in good:
                ext = os.path.splitext(att.filename)[1].lower()
                rel = f"{storage.FILE_PREFIX}{uuid.uuid4().hex}{ext}"
                dest = storage.stored_file_abs(rel)
                await asyncio.to_thread(os.makedirs, storage.FILES_DIR,
                                        exist_ok=True)
                await att.save(dest)
                try:
                    await _db(storage.add_file_song, ctx.author.id, name,
                              att.filename, rel)
                except storage.StorageError as e:
                    await asyncio.to_thread(_delete_stored, [rel])
                    notes.append(str(e))
                    break
                added.append(att.filename)
        if added:
            msg = (f"📁 Stored **{len(added)}** "
                   f"file{'s' if len(added) != 1 else ''} in **{name}**: "
                   + ", ".join(f"**{a}**" for a in added) + ".")
        else:
            msg = f"⚠️ Nothing was stored in **{name}**."
        if notes:
            msg += " " + "; ".join(notes) + "."
        await ctx.send(msg)

    @playlist.command(name="export",
                      help="Export a playlist as a text file to back up or "
                           "share. Bring it back with a!playlist import.")
    async def playlist_export(self, ctx: commands.Context, *, args: str):
        await _ack(ctx)  # slash: the send below carries a file upload
        resolved = await self._resolve_target(ctx, args.split())
        if resolved is None:
            return
        user_id, profile_name, playlist = resolved
        if playlist is None:
            await ctx.send(
                "Which playlist? `a!playlist export [profile] <name>`."
            )
            return
        try:
            songs = await _db(storage.get_songs, user_id, playlist)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if not songs:
            await ctx.send(f"**{profile_name} → {playlist}** is empty.")
            return
        # One song per line: title TAB source TAB duration-in-seconds.
        # Stored files are left out — the disk file belongs to one profile.
        lines = [
            f"{title}\t{source}\t{duration if duration else ''}"
            for title, source, duration, _direct, stored in songs
            if not stored
        ]
        skipped = sum(1 for *_, stored in songs if stored)
        if not lines:
            await ctx.send(
                f"**{profile_name} → {playlist}** only holds stored files, "
                "which can't be exported (their audio lives on this profile)."
            )
            return
        data = ("\n".join(lines) + "\n").encode("utf-8")
        note = (f" ({skipped} stored file{'s' if skipped != 1 else ''} left "
                "out — their audio belongs to this profile)" if skipped else "")
        # Attaching needs the Attach Files permission; without it the upload
        # 403s and the whole reply disappears, so say so instead of going
        # silent (the same for any other upload failure).
        perms = ctx.channel.permissions_for(ctx.me)
        if not perms.attach_files:
            await ctx.send(
                "⚠️ I can't attach files in this channel — give me the "
                "**Attach Files** permission and run this again."
            )
            return
        try:
            await ctx.send(
                f"📄 **{profile_name} → {playlist}** — "
                f"{len(lines)} song{'s' if len(lines) != 1 else ''}{note}. "
                "Bring it into a profile with `a!playlist import <name>`.",
                file=discord.File(io.BytesIO(data), filename=f"{playlist}.txt"),
            )
        except discord.HTTPException as e:
            await ctx.send(f"⚠️ Couldn't upload the export file: {e}")

    @playlist.command(name="import", with_app_command=False,
                      help="Create a playlist from an exported text file: "
                           "attach it to the a!playlist import <name> message "
                           "(a! prefix only).")
    async def playlist_import(self, ctx: commands.Context, *, name: str):
        atts = [a for a in ctx.message.attachments
                if a.filename.lower().endswith(".txt")]
        if not atts:
            await ctx.send(
                "Attach the exported `.txt` file to the `a!playlist import "
                "<name>` message itself. (Prefix only — slash messages can't "
                "carry attachments.)"
            )
            return
        att = atts[0]
        if att.size > MAX_IMPORT_KB * 1024:
            await ctx.send("⚠️ That file is too big to be a playlist export.")
            return
        name = _norm(name)
        if not await self._check_playlist_name(ctx, name):
            return
        try:
            raw = (await att.read()).decode("utf-8", errors="replace")
        except discord.HTTPException:
            await ctx.send("⚠️ Couldn't download the attachment — try again.")
            return
        # Exported lines are title TAB source TAB duration; a bare link per
        # line also works, so hand-written lists import fine.
        songs: list[tuple[str, str, int | None, bool]] = []
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) >= 2 and parts[1]:
                title, source = parts[0] or parts[1], parts[1]
                dur = parts[2] if len(parts) >= 3 else ""
                duration = int(dur) if dur.isdigit() else None
            elif parts[0]:
                title, source, duration = parts[0], parts[0], None
            else:
                continue  # blank line
            songs.append((title, source, duration, False))
        if not songs:
            await ctx.send(
                "⚠️ That file has no songs in it — expected one per line, "
                "like the ones `a!playlist export` makes."
            )
            return
        existing = {
            n.lower() for n, _ in await _db(storage.list_playlists,
                                            ctx.author.id)
        }
        if name.lower() in existing:
            # Refuse-by-default so a shared file can't silently pollute a
            # same-named playlist; the importer can still choose to merge.
            view = _ConfirmView(ctx.author.id)
            prompt = await ctx.send(
                f"**{name}** already exists on your profile — add this "
                f"file's {len(songs)} song{'s' if len(songs) != 1 else ''} "
                "into it? (Songs it already has are skipped.)",
                view=view,
            )
            await view.wait()
            if view.result is None:
                with contextlib.suppress(discord.HTTPException):
                    await prompt.edit(
                        content="Import timed out — nothing was added.",
                        view=None,
                    )
                return
            if view.result is False:
                return  # the Cancel click already updated the prompt
        else:
            try:
                await _db(storage.create_playlist, ctx.author.id, name)
            except storage.StorageError as e:
                await ctx.send(f"⚠️ {e}")
                return
        try:
            added, dupes = await _db(storage.add_songs, ctx.author.id,
                                     name, songs)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        capped = len(songs) - added - dupes
        msg = (f"📥 Imported **{added}** song{'s' if added != 1 else ''} "
               f"into **{name}**")
        notes = []
        if dupes:
            notes.append(f"{dupes} already in it")
        if capped:
            notes.append(f"{capped} over the "
                         f"{storage.PROFILE_SONG_CAP:,}-song cap")
        if notes:
            msg += " (skipped " + ", ".join(notes) + ")"
        await ctx.send(msg + ".")

    @playlist.command(name="view",
                      help="View playlists. No args: your playlists. A profile: "
                           "their playlists. A playlist name: its songs.")
    async def playlist_view(self, ctx: commands.Context, *,
                            args: str | None = None):
        resolved = await self._resolve_target(ctx, args.split() if args else [])
        if resolved is None:
            return
        user_id, profile_name, playlist = resolved
        if playlist is None:
            await self._show_playlists(ctx, user_id, profile_name)
        else:
            await self._show_songs(ctx, user_id, profile_name, playlist)

    @playlist.command(name="playall",
                      help="Queue a whole playlist: a!playlist playall "
                           "[profile] <name> [s] — add s at the end to "
                           "shuffle.")
    async def playlist_playall(self, ctx: commands.Context, *, args: str):
        await _ack(ctx)  # voice connect below can exceed the slash 3 s window
        tokens = args.split()
        shuffle = False
        if tokens and tokens[-1].lower() == "s":  # optional trailing shuffle flag
            shuffle = True
            tokens = tokens[:-1]
        if not tokens:
            await ctx.send(
                "Which playlist? `a!playlist playall [profile] <name> [s]`."
            )
            return
        resolved = await self._resolve_target(ctx, tokens)
        if resolved is None:
            return
        user_id, profile_name, playlist = resolved
        if playlist is None:  # a bare profile name with no playlist after it
            await ctx.send(
                f"Which of **{profile_name}**'s playlists? "
                f"`a!playlist playall {profile_name} <name> [s]`."
            )
            return
        try:
            songs = await _db(storage.get_songs, user_id, playlist)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if not songs:
            await ctx.send(f"**{profile_name} → {playlist}** is empty.")
            return

        music = self.bot.get_cog("Music")
        if not await music.ensure_voice(ctx, join=True):
            return
        player = music.get_player(ctx)
        player.channel = ctx.channel
        if shuffle:
            random.shuffle(songs)
        player.enqueue([
            Track(title=title, source=_track_source(source, stored),
                  duration=duration, requester=ctx.author.display_name,
                  requester_id=ctx.author.id, direct=direct)
            for title, source, duration, direct, stored in songs
        ])
        await ctx.send(
            f"➕ Queued **{len(songs)}** song{'s' if len(songs) != 1 else ''} "
            f"from **{profile_name} → {playlist}**"
            f"{', shuffled' if shuffle else ''}."
        )

    @playlist.command(name="play",
                      help="Queue specific songs from a playlist by number: "
                           "a!playlist play [profile] <name> <n> <n> ...")
    async def playlist_play(self, ctx: commands.Context, *, args: str):
        await _ack(ctx)  # voice connect below can exceed the slash 3 s window
        usage = "a!playlist play [profile] <name> <n> <n> ..."
        tokens = args.split()
        # Whose playlist? Same rule as view/play: a leading profile-name token
        # (profiles are one word) targets that profile, else it's yours.
        user_id, profile_name = ctx.author.id, None
        prof = await _db(storage.find_profile, tokens[0]) if tokens else None
        if prof:
            user_id, profile_name = prof
            tokens = tokens[1:]
        else:
            profile_name = await _db(storage.get_profile, ctx.author.id)
            if profile_name is None:
                await ctx.send(
                    "You don't have a profile yet — create one with "
                    "`a!profile create <name>`."
                )
                return
        # Longest prefix that names one of their playlists (so names ending
        # in numbers, like "Top 40", still work); leftovers are the indexes.
        try:
            match = await self._match_playlist(user_id, tokens)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if match is None:
            await ctx.send(
                f"That doesn't start with one of **{profile_name}**'s "
                f"playlists — check `a!playlist view`." if tokens
                else f"Usage: `{usage}`."
            )
            return
        name, rest = match
        if not rest or not all(t.isdigit() for t in rest):
            await ctx.send(
                f"Usage: `{usage}` — the numbers are the ones shown in "
                f"`a!playlist view {name}`. To queue the whole playlist, "
                f"use `a!playlist playall {name}`."
            )
            return
        indexes = [int(t) for t in rest]
        try:
            songs = await _db(storage.get_songs, user_id, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if not songs:
            await ctx.send(f"**{profile_name} → {name}** is empty.")
            return
        picked = [songs[i - 1] for i in indexes if 1 <= i <= len(songs)]
        skipped = len(indexes) - len(picked)
        if not picked:
            await ctx.send(
                f"None of those numbers are in **{name}** — it has "
                f"{len(songs)} song{'s' if len(songs) != 1 else ''}."
            )
            return

        music = self.bot.get_cog("Music")
        if not await music.ensure_voice(ctx, join=True):
            return
        player = music.get_player(ctx)
        player.channel = ctx.channel
        player.enqueue([
            Track(title=title, source=_track_source(source, stored),
                  duration=duration, requester=ctx.author.display_name,
                  requester_id=ctx.author.id, direct=direct)
            for title, source, duration, direct, stored in picked
        ])
        msg = (
            f"➕ Queued **{len(picked)}** song{'s' if len(picked) != 1 else ''} "
            f"from **{profile_name} → {name}**."
        )
        if skipped:
            msg += (f" Skipped {skipped} number{'s' if skipped != 1 else ''} "
                    f"outside 1–{len(songs)}.")
        await ctx.send(msg)

    @staticmethod
    async def _split_trailing_ints(ctx: commands.Context, args: str,
                                   count: int, usage: str
                                   ) -> tuple[str, list[int]] | None:
        """Split '<name with spaces> <int>...' into the name and the trailing
        integers. Sends the usage line and returns None if it doesn't fit."""
        tokens = args.split()
        name, nums = " ".join(tokens[:-count]), tokens[-count:]
        if len(tokens) <= count or not all(
            t.isdigit() for t in nums
        ):
            await ctx.send(f"Usage: `{usage}`.")
            return None
        return _norm(name), [int(t) for t in nums]

    @playlist.command(name="move",
                      help="Move song N to slot M in your playlist: "
                           "a!playlist move <name> <from> <to>.")
    async def playlist_move(self, ctx: commands.Context, *, args: str):
        parsed = await self._split_trailing_ints(
            ctx, args, 2, "a!playlist move <name> <from> <to>")
        if parsed is None:
            return
        name, (frm, to) = parsed
        try:
            title = await _db(storage.move_song, ctx.author.id, name, frm, to)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        await ctx.send(f"⏫ Moved **{title}** to slot {to} in **{name}**.")

    @playlist.command(name="swap",
                      help="Swap songs N and M in your playlist: "
                           "a!playlist swap <name> <x> <y>.")
    async def playlist_swap(self, ctx: commands.Context, *, args: str):
        parsed = await self._split_trailing_ints(
            ctx, args, 2, "a!playlist swap <name> <x> <y>")
        if parsed is None:
            return
        name, (a, b) = parsed
        try:
            t1, t2 = await _db(storage.swap_songs, ctx.author.id, name, a, b)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        await ctx.send(f"🔃 Swapped **{t1}** ↔ **{t2}** in **{name}**.")

    @playlist.command(name="remove",
                      help="Remove songs by their view numbers — "
                           "a!playlist remove <name> 3, or 3 9 14 for several.")
    async def playlist_remove(self, ctx: commands.Context, *, args: str):
        usage = "a!playlist remove <name> <n> [n ...]"
        # Longest prefix that names one of your playlists (so names ending
        # in numbers, like "Top 40", still work); leftovers are the slots.
        try:
            match = await self._match_playlist(ctx.author.id, args.split())
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if match is None:
            await ctx.send(
                "That doesn't start with one of your playlists — check "
                "`a!playlist view`."
            )
            return
        name, rest = match
        if not rest or not all(t.isdigit() for t in rest):
            await ctx.send(f"Usage: `{usage}`.")
            return
        indexes = sorted({int(t) for t in rest})
        try:
            valid, titles, files = await _db(storage.remove_songs,
                                             ctx.author.id, name, indexes)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if files:
            await asyncio.to_thread(_delete_stored, files)
        if len(titles) == 1:
            msg = f"🗑️ Removed **{titles[0]}** from **{name}**."
        else:
            msg = (f"🗑️ Removed **{len(titles)}** songs (slots "
                   f"{', '.join(str(i) for i in valid)}) from **{name}**.")
        skipped = len(indexes) - len(valid)
        if skipped:
            msg += (f" Skipped {skipped} number{'s' if skipped != 1 else ''} "
                    f"outside the playlist.")
        await ctx.send(msg)

    @playlist.command(name="removerange",
                      help="Remove songs X through Y from a playlist — "
                           "a!playlist removerange <name> 2 5 (or 2-5).")
    async def playlist_remove_range(self, ctx: commands.Context, *, args: str):
        usage = "a!playlist removerange <name> <x> <y>"
        # Same two spellings as a!removerange: trailing "2 5" or "2-5".
        tokens = args.split()
        m = re.fullmatch(r"(\d+)-(\d+)", tokens[-1]) if tokens else None
        if m and len(tokens) >= 2:
            name = _norm(" ".join(tokens[:-1]))
            start, end = int(m.group(1)), int(m.group(2))
        else:
            parsed = await self._split_trailing_ints(ctx, args, 2, usage)
            if parsed is None:
                return
            name, (start, end) = parsed
        if start > end:
            await ctx.send(
                "The first slot must be less than or equal to the second.")
            return
        try:
            titles, files = await _db(storage.remove_song_range,
                                      ctx.author.id, name, start, end)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if files:
            await asyncio.to_thread(_delete_stored, files)
        if len(titles) == 1:
            await ctx.send(f"🗑️ Removed **{titles[0]}** from **{name}**.")
        else:
            await ctx.send(
                f"🗑️ Removed **{len(titles)}** songs (slots {start}–{end}) "
                f"from **{name}**."
            )

    @playlist.command(name="rename",
                      help="Rename a playlist: a!playlist rename <old> <new>.")
    async def playlist_rename(self, ctx: commands.Context, *, args: str):
        # The old name must exist, so match it off the front (longest match
        # wins, same as add); whatever's left is the new name.
        try:
            match = await self._match_playlist(ctx.author.id, args.split())
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if match is None:
            await ctx.send(
                "That doesn't start with one of your playlists — check "
                "`a!playlist view`."
            )
            return
        old, rest = match
        new = _norm(" ".join(rest))
        if not new:
            await ctx.send(f"Rename **{old}** to what? "
                           f"`a!playlist rename {old} <new name>`.")
            return
        if not await self._check_playlist_name(ctx, new):
            return
        try:
            await _db(storage.rename_playlist, ctx.author.id, old, new)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        await ctx.send(f"✅ Renamed **{old}** → **{new}**.")

    @playlist.command(name="clear",
                      help="Remove every song from one of your playlists "
                           "(keeps the playlist).")
    async def playlist_clear(self, ctx: commands.Context, *, name: str):
        name = _norm(name)
        try:
            removed, files = await _db(storage.clear_songs, ctx.author.id, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if files:
            await asyncio.to_thread(_delete_stored, files)
        if removed == 0:
            await ctx.send(f"**{name}** is already empty.")
        else:
            await ctx.send(
                f"🧹 Cleared **{removed}** song{'s' if removed != 1 else ''} "
                f"from **{name}**."
            )

    @playlist.command(name="delete", help="Delete one of your playlists.")
    async def playlist_delete(self, ctx: commands.Context, *, name: str):
        name = _norm(name)
        try:
            files = await _db(storage.delete_playlist, ctx.author.id, name)
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if files:
            await asyncio.to_thread(_delete_stored, files)
        await ctx.send(f"🗑️ Playlist **{name}** deleted.")

    @commands.hybrid_command(help="Save the currently playing song to one of "
                                  "your playlists.")
    async def save(self, ctx: commands.Context, *, playlist: str):
        playlist = _norm(playlist)
        music = self.bot.get_cog("Music")
        player = music.players.get(ctx.guild.id) if ctx.guild else None
        if player is None or player.current is None:
            await ctx.send("Nothing is playing to save.")
            return
        t = player.current
        if t.direct and "discordapp" in t.source:
            await ctx.send(
                "⚠️ This song came from an attached file — those can't be "
                "saved (Discord attachment links expire after ~24h). If you "
                "have file storage, use `a!playlist addfile` instead."
            )
            return
        if t.direct and storage.is_stored_file(t.source):
            # A stored file belongs to one profile: a second row pointing at
            # the same disk file would break when either copy is removed.
            await ctx.send(
                "⚠️ This song is a stored file on its owner's profile — "
                "stored files can't be copied into other playlists."
            )
            return
        try:
            # add_songs raises with the right message if the user has no
            # profile or no playlist by that name.
            added, _ = await _db(storage.add_songs, ctx.author.id, playlist,
                                 [(t.title, t.source, t.duration, t.direct)])
        except storage.StorageError as e:
            await ctx.send(f"⚠️ {e}")
            return
        if added == 0:
            await ctx.send(f"**{t.title}** is already in **{playlist}**.")
            return
        await ctx.send(f"💾 Saved **{t.title}** to **{playlist}**.")

    # -- file-storage permissions (owner-only, prefix-only; see a!devhelp) --------

    @commands.command(name="grantfiles",
                      help="(Owner only) Let a user store audio files with "
                           "a!playlist addfile.")
    @commands.is_owner()
    async def grantfiles(self, ctx: commands.Context, user: discord.User):
        changed = await _db(storage.set_file_perm, user.id, True)
        if changed:
            await ctx.send(
                f"📁 {user.mention} can now store audio files: drag them "
                f"into an `a!playlist addfile <playlist name>` message "
                f"({MAX_FILE_MB} MB/file, "
                f"{storage.PROFILE_FILE_CAP} files per profile)."
            )
        else:
            await ctx.send(f"{user.mention} already has file storage.")

    @commands.command(name="revokefiles",
                      help="(Owner only) Stop a user from storing new audio "
                           "files (their existing files stay).")
    @commands.is_owner()
    async def revokefiles(self, ctx: commands.Context, user: discord.User):
        changed = await _db(storage.set_file_perm, user.id, False)
        if changed:
            await ctx.send(
                f"📁 {user.mention} can no longer store new files "
                "(their existing files still play)."
            )
        else:
            await ctx.send(f"{user.mention} didn't have file storage.")

    @commands.command(name="fileperms",
                      help="(Owner only) List users with file storage.")
    @commands.is_owner()
    async def fileperms(self, ctx: commands.Context):
        ids = await _db(storage.list_file_perms)
        if not ids:
            await ctx.send("Nobody has file storage yet — grant it with "
                           "`a!grantfiles @user`.")
            return
        await ctx.send(
            "📁 File storage: " + ", ".join(f"<@{i}>" for i in ids),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="storage",
                      help="(Owner only) Disk usage: database, stored audio "
                           "files, and who they belong to.")
    @commands.is_owner()
    async def storage_cmd(self, ctx: commands.Context):
        db_bytes, n_files, files_bytes, rows = await _db(storage.storage_report)

        def mb(n: int) -> str:
            return f"{n / (1024 * 1024):,.2f} MB"

        lines = [
            f"💽 Database (`auxly.db`): **{mb(db_bytes)}**",
            f"📁 Stored audio files (`audio_files/`): **{n_files}** "
            f"file{'s' if n_files != 1 else ''}, **{mb(files_bytes)}**",
        ]
        if rows:
            lines.append("")
            for name, songs, files, fbytes in rows:
                lines.append(
                    f"**{name}** — {files}/{storage.PROFILE_FILE_CAP} files "
                    f"({mb(fbytes)}), {songs:,}/{storage.PROFILE_SONG_CAP:,} "
                    f"songs"
                )
        await ctx.send("\n".join(lines))

    # -- help --------------------------------------------------------------------

    @commands.hybrid_command(name="profilehelp",
                             help="List all profile & playlist commands.")
    async def profile_help(self, ctx: commands.Context):
        lines = [
            "✨ Every command also works as a **/slash command** — same names, "
            "same behavior.\n",
            "**Profiles** — one per Discord account, follows you across servers.",
            "`a!profile create <name>` — Create your profile.",
            "`a!profile rename <newname>` — Rename your profile.",
            "`a!profiles` — List all profiles.",
            "",
            "**Playlists** — saved song collections on your profile. Anyone can "
            "view and queue them; only you can change them.",
            "`a!playlist create <name>` — New playlist.",
            "`a!playlist add <name> <song>` — Add anything `a!play` accepts "
            "(links, searches, whole YouTube/Spotify playlists) except attached "
            "files.",
            "`a!playlist addfile <name>` — Store attached audio files in your "
            "playlist (requires file perms from the bot owner; `a!` prefix "
            "only).",
            "`a!playlist export [profile] <name>` — Get a playlist as a "
            "text file you can back up or share (stored files left out).",
            "`a!playlist import <name>` — Create a playlist from an exported "
            "file — attach it to the message (`a!` prefix only). If the name "
            "already exists you'll be asked before songs are added to it.",
            "`a!playlist view` — List your playlists.",
            "`a!playlist view <profile>` — List someone's playlists.",
            "`a!playlist view [profile] <name>` — Show a playlist's songs.",
            "`a!playlist playall [profile] <name> [s]` — Queue a whole "
            "playlist. Add `s` at the end to shuffle it in — optional, leave "
            "it off for normal order.",
            "`a!playlist play [profile] <name> <n> <n> ...` — Queue just "
            "those numbered songs (numbers from `a!playlist view`), in the "
            "order you list them.",
            "`a!playlist move <name> <from> <to>` — Move a song to another slot.",
            "`a!playlist swap <name> <x> <y>` — Swap two songs' slots.",
            "`a!playlist remove <name> <n> [n ...]` — Remove songs by number; "
            "`a!playlist remove ost 3 9 14` removes all three.",
            "`a!playlist removerange <name> <x> <y>` — Remove songs x through "
            "y (`2 5` or `2-5` both work).",
            "`a!save <playlist>` — Save the **currently playing** song to "
            "your playlist.",
            "`a!playlist rename <old> <new>` — Rename your playlist.",
            "`a!playlist clear <name>` — Remove all songs (keeps the playlist).",
            "`a!playlist delete <name>` — Delete your playlist.",
        ]
        embed = discord.Embed(
            title="👤 Profile & Playlist Commands",
            description="\n".join(lines),
            color=EMBED_COLOR,
        )
        embed.set_footer(text="Profile names are one word; playlist names can "
                              "contain spaces (no quotes needed anywhere).")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profiles(bot))
