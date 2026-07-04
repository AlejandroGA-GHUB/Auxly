"""Music cog: all commands + per-guild player state.

Audio path (the whole point of this bot):
  yt-dlp bestaudio (Opus preferred) -> ffprobe codec check -> Discord.
Opus sources are stream-COPIED (zero re-encode). Everything else gets exactly
one libopus encode at 510 kbps with discord.py's hardcoded FEC/packet-loss
tax overridden (see FFMPEG_ENCODE_OPTIONS). No filters ever touch the stream.
"""

import asyncio
import contextlib
import json
import random
import re
import subprocess
import sys
import time
from collections import deque

import discord
from discord.ext import commands

import sources
from sources import Track, TrackError

IDLE_TIMEOUT = 60 * 60  # 1 hour with nothing playing -> disconnect
EMPTY_TIMEOUT = 5 * 60  # empty voice channel: pause, then leave after 5 min
# Playback dying faster than this (on a track that should be longer) means
# FFmpeg never really started — usually a stream URL YouTube 403'd (it does
# that intermittently on fresh URLs). One retry with a re-extracted URL
# fixes the transient cases; discord.py itself reports it as a normal
# end-of-stream, so without this check the song just silently "skips".
FAILED_START_SECS = 5

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
# discord.py hardcodes '-fec true -packet_loss 15' into every Opus encode —
# a speech-robustness mode that measurably costs ~11 dB SNR and a third of
# the effective bitrate. These trailing flags override it (FFmpeg last-wins).
# Only the encode path needs this; codec-copy never runs the encoder.
FFMPEG_ENCODE_OPTIONS = FFMPEG_OPTIONS + " -fec 0 -packet_loss 0"

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _probe_codec_sync(url: str) -> str | None:
    """Audio codec of a source, via ffprobe. Same check as
    discord.FFmpegOpusAudio.probe, but spawned with CREATE_NO_WINDOW —
    discord.py's probe omits it (its ffmpeg player doesn't), so under the
    windowless launcher a console window flashed at every song start."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a:0", url],
        timeout=20, creationflags=_NO_WINDOW,
    )
    streams = json.loads(out).get("streams", [])
    return streams[0].get("codec_name") if streams else None


async def probe_codec(url: str) -> str | None:
    return await asyncio.to_thread(_probe_codec_sync, url)


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class NowPlayingView(discord.ui.View):
    """Pause/Resume · Loop · Skip buttons under a Now Playing message.

    Behavior is always computed from live player state at click time; labels
    are refreshed on every interaction, so a stale label can never misfire.
    """

    def __init__(self, player: "GuildPlayer", track: Track):
        super().__init__(timeout=None)
        self.player = player
        self.track = track
        self._sync_task: asyncio.Task | None = None  # debounced label update
        self.refresh()

    def refresh(self):
        if self.is_finished():  # retired: never resurrect labels/buttons
            return
        vc = self.player.guild.voice_client
        paused = vc is not None and vc.is_paused()
        self.pause_button.label = "Resume" if paused else "Pause"
        self.pause_button.emoji = "▶️" if paused else "⏸️"
        # Each Loop click adds one repeat; a Cancel button exists only while
        # a loop is active.
        n = self.player.loops_left
        self.loop_button.label = f"Loop ({n})" if n else "Loop"
        has_cancel = self.cancel_button in self.children
        if n and not has_cancel:
            self.add_item(self.cancel_button)
        elif not n and has_cancel:
            self.remove_item(self.cancel_button)

    def disable_all(self):
        if self._sync_task is not None:
            self._sync_task.cancel()
            self._sync_task = None
        for item in self.children:
            item.disabled = True
        self.stop()

    def schedule_sync(self, message: discord.Message, delay: float = 0.35):
        """Debounced view refresh: rapid clicks collapse into one edit,
        instead of queueing an API round trip per click."""
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()
        self._sync_task = asyncio.get_running_loop().create_task(
            self._sync_after(message, delay)
        )

    async def _sync_after(self, message: discord.Message, delay: float):
        await asyncio.sleep(delay)
        self.refresh()
        try:
            await message.edit(view=self)
            if self.is_finished():
                # Retired while our edit was in flight: our enabled snapshot
                # may have landed after the grey-out — re-apply disabled.
                await message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _edit(self, interaction: discord.Interaction):
        """Respond to a click by re-rendering the view — and if the song
        ended while that edit was in flight (retire_controls races with
        interaction responses), re-apply the greyed-out state so a finished
        song can never be left with live-looking buttons."""
        await interaction.response.edit_message(view=self)
        if self.is_finished():
            try:
                await interaction.edit_original_response(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = self.player
        vc = player.guild.voice_client
        if (player.task.done() or player.current is not self.track
                or vc is None or not vc.is_connected()):
            self.disable_all()
            try:
                await interaction.response.edit_message(view=self)
            except discord.HTTPException:
                pass
            return False
        member = interaction.user
        if member.voice is None or member.voice.channel != vc.channel:
            await interaction.response.send_message(
                "You need to be in **my** voice channel to control me.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Pause", emoji="⏸️",
                       style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        vc = self.player.guild.voice_client
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        self.refresh()
        await self._edit(interaction)

    @discord.ui.button(label="Loop", emoji="🔁",
                       style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        # Plain edit-per-click: slower than a defer+debounce, but the label
        # updates in lockstep with the click (owner prefers the reliability).
        self.player.loops_left += 1  # one more repeat per click
        self.refresh()
        await self._edit(interaction)

    @discord.ui.button(label="Skip", emoji="⏭️",
                       style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        player = self.player
        player.loops_left = 0  # skip cancels a!loop, same as the command
        player.current = None
        player.guild.voice_client.stop()
        self.disable_all()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel Loop", emoji="✖️",
                       style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        self.player.loops_left = 0
        self.refresh()
        await self._edit(interaction)


PAGE_SIZE = 20  # list entries per page in a!queue / a!playlist view


class PageView(discord.ui.View):
    """Prev/Next buttons under a long list embed (a!queue, a!playlist view).

    `render(page)` returns (embed, page_count) and is called fresh on every
    click, so live lists (the queue) always show current contents. Pure
    embed edits on the event loop — never touches the audio path.
    """

    def __init__(self, render):
        super().__init__(timeout=600)  # 10 min, then the buttons grey out
        self.render = render
        self.page = 0
        self.message: discord.Message | None = None
        embed, self.page_count = render(0)
        self.first_embed = embed
        self._sync_buttons()

    def _sync_buttons(self):
        at_start = self.page <= 0
        at_end = self.page >= self.page_count - 1
        self.first_button.disabled = at_start
        self.prev_button.disabled = at_start
        self.next_button.disabled = at_end
        self.last_button.disabled = at_end

    async def _goto(self, interaction: discord.Interaction, page: int):
        self.page = page
        embed, self.page_count = self.render(self.page)
        if self.page >= self.page_count:  # list shrank since last look
            self.page = self.page_count - 1
            embed, self.page_count = self.render(self.page)
        self.page = max(0, self.page)
        self._sync_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="« First", style=discord.ButtonStyle.secondary)
    async def first_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        await self._goto(interaction, 0)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await self._goto(interaction, self.page - 1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await self._goto(interaction, self.page + 1)

    @discord.ui.button(label="Last »", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await self._goto(interaction, self.page_count - 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def send(self, ctx: commands.Context):
        """Send the first page; attach buttons only if there are more pages."""
        if self.page_count > 1:
            self.message = await ctx.send(embed=self.first_embed, view=self)
        else:
            self.stop()
            await ctx.send(embed=self.first_embed)


class GuildPlayer:
    """One per guild: FIFO queue + playback loop task."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild,
                 channel: discord.abc.Messageable):
        self.bot = bot
        self.guild = guild
        self.channel = channel  # text channel for announcements
        self.queue: deque[Track] = deque()
        self.queue_updated = asyncio.Event()
        self.current: Track | None = None
        self.loops_left = 0  # extra plays of current track (a!loop <n>)
        self.started_at: float | None = None
        self.np_message: discord.Message | None = None  # latest controls msg
        self.np_view: NowPlayingView | None = None
        self.empty_task: asyncio.Task | None = None  # empty-channel countdown
        self.auto_paused = False  # paused by us (empty channel), not a user
        # (track, stream_url, codec) of the current song, so loop replays
        # skip the yt-dlp extraction + ffprobe (no gap, no CPU churn).
        self._stream_cache: tuple[Track, str, str | None] | None = None
        self.task = bot.loop.create_task(self.player_loop())

    # -- queue helpers ------------------------------------------------------

    def enqueue(self, tracks: list[Track]):
        self.queue.extend(tracks)
        self.queue_updated.set()

    def enqueue_front(self, tracks: list[Track]):
        self.queue.extendleft(reversed(tracks))  # keep the tracks' order
        self.queue_updated.set()

    # -- empty-channel handling ---------------------------------------------

    def on_channel_empty_changed(self, empty: bool):
        """Pause + start a leave countdown when the voice channel empties;
        cancel it (and resume, if we were the ones who paused) on return."""
        if empty and self.empty_task is None:
            self.empty_task = self.bot.loop.create_task(self._empty_countdown())
        elif not empty and self.empty_task is not None:
            self.empty_task.cancel()
            self.empty_task = None
            vc = self.guild.voice_client
            if self.auto_paused and vc and vc.is_paused():
                vc.resume()
            self.auto_paused = False

    async def _empty_countdown(self):
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            self.auto_paused = True
        await asyncio.sleep(EMPTY_TIMEOUT)
        self.empty_task = None
        await self._say("👋 Everyone left the voice channel — heading out.")
        self.destroy()

    async def _next_track(self) -> Track:
        """Pop the next track, waiting up to IDLE_TIMEOUT before giving up."""
        deadline = time.monotonic() + IDLE_TIMEOUT
        while not self.queue:
            self.queue_updated.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                await asyncio.wait_for(self.queue_updated.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise
        return self.queue.popleft()

    # -- playback loop ------------------------------------------------------

    async def player_loop(self):
        try:
            while True:
                if self.current is not None and self.loops_left > 0:
                    self.loops_left -= 1
                    track = self.current  # replay, queue is held
                else:
                    self.current = None
                    self.loops_left = 0
                    try:
                        track = await self._next_track()
                    except asyncio.TimeoutError:
                        await self._say("💤 Idle for an hour — leaving the channel. See ya!")
                        break
                await self._play(track)
        except asyncio.CancelledError:
            raise
        finally:
            self.bot.loop.create_task(self._teardown())

    async def _play(self, track: Track):
        for attempt in (1, 2):
            vc = self.guild.voice_client
            if vc is None or not vc.is_connected():
                raise asyncio.CancelledError  # disconnected externally; shut down

            try:
                cached = self._stream_cache
                if cached is not None and cached[0] is track:
                    _, stream_url, codec = cached  # loop replay: reuse, no re-fetch
                else:
                    stream_url = await sources.get_stream_url(track)
                    codec = None
                    for _ in range(2):  # one retry: a transient ffprobe hiccup
                        try:            # must never force a re-encode
                            codec = await probe_codec(stream_url)
                            break
                        except Exception:
                            pass
                    self._stream_cache = (track, stream_url, codec)
                # -reconnect* are HTTP protocol options; on a local file
                # (stored upload) FFmpeg rejects them and dies instantly.
                before = (FFMPEG_BEFORE
                          if stream_url.lower().startswith("http") else None)
                if codec in ("opus", "libopus"):
                    # Source is already Opus: copy the bitstream, zero re-encode.
                    source = discord.FFmpegOpusAudio(
                        stream_url,
                        codec="copy",
                        before_options=before,
                        options=FFMPEG_OPTIONS,
                    )
                else:
                    # Anything else (mp3/flac/wav/m4a…): one clean encode at max
                    # Opus bitrate, with discord.py's forced FEC tax disabled.
                    source = discord.FFmpegOpusAudio(
                        stream_url,
                        bitrate=510,
                        before_options=before,
                        options=FFMPEG_ENCODE_OPTIONS,
                    )
            except TrackError as e:
                self._stream_cache = None
                await self._say(f"⚠️ Skipping **{track.title}**: {e}")
                return
            except Exception as e:
                self._stream_cache = None
                await self._say(f"⚠️ Skipping **{track.title}** (playback error: {e})")
                return

            finished = asyncio.Event()
            play_error = None

            def after(err):
                nonlocal play_error
                play_error = err
                if err:
                    print(f"[player] {self.guild.name}: {err}")
                self.bot.loop.call_soon_threadsafe(finished.set)

            replay = track is self.current
            self.current = track
            self.started_at = time.monotonic()
            vc.play(source, after=after)
            if not replay:
                Music.histories.setdefault(self.guild.id, deque(maxlen=10)).append(track)
                await self.post_controls(
                    content=f"🎶 Now playing: **{track.title}** "
                            f"[{fmt_duration(track.duration)}] "
                            f"(queued by {track.requester})"
                )
            else:
                self.sync_controls()  # a repeat was consumed; count down the label
            await finished.wait()

            if self.current is not track:
                break  # a user skipped it; don't second-guess them
            elapsed = time.monotonic() - self.started_at
            died_early = (
                elapsed < FAILED_START_SECS
                and (track.duration is None or track.duration > FAILED_START_SECS * 3)
            )
            if play_error is None and not died_early:
                break  # played out normally
            # Failed start. The URL is bad — never reuse it (a loop replay
            # would just fail again from cache). Direct file links can't be
            # re-extracted, and a short no-error "death" may simply be a
            # short clip, so they don't retry.
            self._stream_cache = None
            if track.direct:
                if play_error is not None:
                    await self._say(
                        f"⚠️ **{track.title}** failed to play ({play_error}) — skipping."
                    )
                break
            if attempt == 2:
                detail = f" ({play_error})" if play_error else " (stream error)"
                await self._say(
                    f"⚠️ **{track.title}** failed to start{detail} — skipping."
                )
                self.loops_left = 0  # a dead track must not loop
                break

        if self.loops_left == 0:  # song is over for good; controls with it
            await self.retire_controls()

    async def _say(self, msg: str):
        try:
            await self.channel.send(msg)
        except discord.HTTPException:
            pass

    def sync_controls(self):
        """Refresh the button labels (debounced) after loop state changes
        outside a button click — a!loop, or a replay consuming a repeat."""
        if self.np_view is not None and self.np_message is not None:
            self.np_view.schedule_sync(self.np_message)

    async def retire_controls(self):
        """Grey out the previous Now Playing buttons (only the newest
        message keeps live controls)."""
        view, msg = self.np_view, self.np_message
        self.np_view = self.np_message = None
        if view is None:
            return
        view.disable_all()
        if msg:
            try:
                await msg.edit(view=view)
            except discord.HTTPException:
                pass

    async def post_controls(self, *, content: str | None = None,
                            embed: discord.Embed | None = None,
                            sender=None):
        """Send a message with the Pause/Loop/Skip buttons for the current
        track, retiring any previous controls. `sender` lets a command pass
        ctx.send so a slash invocation gets its interaction response."""
        await self.retire_controls()
        if self.current is None:
            return
        view = NowPlayingView(self, self.current)
        try:
            self.np_message = await (sender or self.channel.send)(
                content=content, embed=embed, view=view
            )
            self.np_view = view
        except discord.HTTPException:
            pass

    async def _teardown(self):
        await self.retire_controls()
        vc = self.guild.voice_client
        if vc:
            await vc.disconnect(force=True)
        Music.players.pop(self.guild.id, None)

    def destroy(self):
        if self.empty_task is not None:
            self.empty_task.cancel()
            self.empty_task = None
        self.task.cancel()


class Music(commands.Cog):
    players: dict[int, GuildPlayer] = {}
    # Last 10 played per guild. In-memory only (dies with the process, by
    # design) but outlives a leave/rejoin within a session.
    histories: dict[int, deque[Track]] = {}

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        sources.prewarm()  # spawn yt-dlp workers before any audio plays

    # -- plumbing -----------------------------------------------------------

    def get_player(self, ctx: commands.Context) -> GuildPlayer:
        player = self.players.get(ctx.guild.id)
        if player is None or player.task.done():
            player = GuildPlayer(self.bot, ctx.guild, ctx.channel)
            self.players[ctx.guild.id] = player
        return player

    @staticmethod
    async def ack(ctx: commands.Context):
        """Slash invocations must be acknowledged within 3 s; voice connect
        and yt-dlp resolution can take longer. No-op for prefix commands."""
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

    @staticmethod
    def progress(ctx: commands.Context):
        """Typing indicator for prefix invocations; slash contexts are
        already deferred (ctx.typing() would double-defer and raise)."""
        return ctx.typing() if ctx.interaction is None else contextlib.nullcontext()

    @staticmethod
    async def ensure_voice(ctx: commands.Context, join: bool = False) -> bool:
        """Check the author is in voice; optionally join their channel."""
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.")
            return False
        vc = ctx.voice_client
        if vc is None or not vc.is_connected():
            if not join:
                await ctx.send("I'm not playing anything right now.")
                return False
            await ctx.author.voice.channel.connect(self_deaf=True)
        elif vc.channel != ctx.author.voice.channel:
            await ctx.send("You need to be in **my** voice channel to control me.")
            return False
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # If the bot itself got disconnected, tear the player down.
        if member.id == self.bot.user.id and after.channel is None:
            player = self.players.get(member.guild.id)
            if player:
                player.destroy()
            return
        # Track whether the bot's channel has any humans left in it.
        player = self.players.get(member.guild.id)
        vc = member.guild.voice_client
        if player is None or vc is None or vc.channel is None:
            return
        if vc.channel not in (before.channel, after.channel):
            return  # movement elsewhere; our channel's headcount is unchanged
        empty = not any(not m.bot for m in vc.channel.members)
        player.on_channel_empty_changed(empty)

    # -- commands -----------------------------------------------------------

    async def _enqueue_request(self, ctx: commands.Context,
                               query: str | None, front: bool):
        """Shared body of play/playnext: resolve input and queue it."""
        attachments = [
            a for a in (ctx.message.attachments if ctx.message else [])
            if a.filename.lower().endswith(sources.AUDIO_EXTS)
        ]  # slash invocations have no message to attach files to
        if not query and not attachments:
            await ctx.send(
                "Give me something to play: a link, search terms, or an "
                "attached audio file."
            )
            return
        await self.ack(ctx)
        if not await self.ensure_voice(ctx, join=True):
            return
        player = self.get_player(ctx)
        player.channel = ctx.channel
        tracks = [
            Track(title=a.filename, source=a.url,
                  requester=ctx.author.display_name, direct=True)
            for a in attachments
        ]
        if query:
            async with self.progress(ctx):
                try:
                    tracks += await sources.resolve(query, ctx.author.display_name)
                except TrackError as e:
                    await ctx.send(f"⚠️ {e}")
                    if not tracks:
                        return
        for t in tracks:
            t.requester = ctx.author.display_name
        busy = player.current is not None or player.queue
        if front:
            player.enqueue_front(tracks)
        else:
            player.enqueue(tracks)
        if len(tracks) > 1:
            if front:
                await ctx.send(f"⏫ Queued **{len(tracks)}** tracks to play next.")
            else:
                await ctx.send(f"➕ Queued **{len(tracks)}** tracks.")
        elif busy:
            if front:
                await ctx.send(f"⏫ **{tracks[0].title}** will play next.")
            else:
                await ctx.send(
                    f"➕ Queued **{tracks[0].title}** (position {len(player.queue)})."
                )
        elif ctx.interaction is not None:
            # A slash invocation must get a response; prefix stays quiet here
            # because the "Now playing" announcement covers it.
            await ctx.send(f"🎶 Starting **{tracks[0].title}**.")

    @commands.hybrid_command(brief="Play a song or add it to the queue.",
                           help="Play a song or add it to the queue. Takes a YouTube "
                           "link, Spotify link, playlist link, search terms, or "
                           "an attached audio file (mp3, wav, flac, …).")
    async def play(self, ctx: commands.Context, *, query: str | None = None):
        await self._enqueue_request(ctx, query, front=False)

    @commands.hybrid_command(help="Like play, but puts it at the FRONT of the queue "
                           "(plays right after the current song).")
    async def playnext(self, ctx: commands.Context, *, query: str | None = None):
        await self._enqueue_request(ctx, query, front=True)

    @commands.command(help="Same as play — adds a song/playlist to the queue.")
    async def queue_add(self, ctx: commands.Context, *, query: str):
        await self.play(ctx, query=query)

    @commands.hybrid_command(help="Bring the bot into your voice channel "
                           "without playing anything.")
    async def join(self, ctx: commands.Context):
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.")
            return
        await self.ack(ctx)  # voice connect can exceed the slash 3 s window
        target = ctx.author.voice.channel
        vc = ctx.voice_client
        if vc is not None and vc.is_connected():
            if vc.channel == target:
                await ctx.send("I'm already in your channel.")
            elif vc.is_playing() or vc.is_paused():
                await ctx.send(
                    "I'm busy playing in another channel — "
                    "stop me there first (`a!stop`)."
                )
            else:
                await vc.move_to(target)
                await ctx.send(f"👋 Moved to **{target.name}**.")
            return
        await target.connect(self_deaf=True)
        # Start a player so the usual idle/empty-channel timers apply.
        self.get_player(ctx).channel = ctx.channel
        await ctx.send(f"👋 Joined **{target.name}**.")

    @commands.hybrid_command(help="Pause the current song.")
    async def pause(self, ctx: commands.Context):
        if not await self.ensure_voice(ctx):
            return
        vc = ctx.voice_client
        if vc.is_playing():
            vc.pause()
            await ctx.send("⏸️ Paused.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.hybrid_command(help="Resume a paused song.")
    async def resume(self, ctx: commands.Context):
        if not await self.ensure_voice(ctx):
            return
        vc = ctx.voice_client
        if vc.is_paused():
            vc.resume()
            await ctx.send("▶️ Resumed.")
        else:
            await ctx.send("Nothing is paused.")

    @commands.hybrid_command(help="Skip the current song (also cancels an active loop).")
    async def skip(self, ctx: commands.Context):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        vc = ctx.voice_client
        if player is None or (not vc.is_playing() and not vc.is_paused()):
            await ctx.send("Nothing to skip.")
            return
        player.loops_left = 0  # skip cancels a!loop
        player.current = None
        vc.stop()
        await ctx.send("⏭️ Skipped.")

    @commands.hybrid_command(brief="Repeat the current song N more times.",
                           help="Repeat the current song N more times. The queue waits "
                           "until the loops finish (or someone skips). a!loop 0 cancels.")
    async def loop(self, ctx: commands.Context, count: int):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is None or player.current is None:
            await ctx.send("Nothing is playing to loop.")
            return
        if count < 0:
            await ctx.send("The loop count can't be negative.")
            return
        player.loops_left = count
        player.sync_controls()  # keep the button label in step
        if count == 0:
            await ctx.send("🔁 Loop cancelled.")
        else:
            await ctx.send(
                f"🔁 **{player.current.title}** will play **{count}** more "
                f"time{'s' if count != 1 else ''} before the queue continues."
            )

    def _queue_embed(self, guild_id: int, page: int) -> tuple[discord.Embed, int]:
        """One page of the queue, rendered from live state. Returns
        (embed, page_count) — the PageView calls this on every click."""
        player = self.players.get(guild_id)
        if player is None or (player.current is None and not player.queue):
            return discord.Embed(
                title="Queue", description="The queue is empty.",
                color=0x5865F2), 1
        pending = list(player.queue)
        pages = max(1, (len(pending) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        lines = []
        if player.current:
            loop_note = f" (looping {player.loops_left}x more)" if player.loops_left else ""
            lines.append(
                f"▶️ **{player.current.title}** "
                f"[{fmt_duration(player.current.duration)}]{loop_note}"
            )
        start = page * PAGE_SIZE
        for i, t in enumerate(pending[start:start + PAGE_SIZE], start=start + 1):
            lines.append(f"`{i}.` {t.title} [{fmt_duration(t.duration)}] — {t.requester}")
        embed = discord.Embed(
            title="Queue", description="\n".join(lines), color=0x5865F2)
        if pending:
            total = sum(t.duration or 0 for t in pending)
            if player.current and player.current.duration and player.started_at:
                elapsed = time.monotonic() - player.started_at
                total += max(0, player.current.duration - int(elapsed))
                total += player.loops_left * player.current.duration
            n = len(pending)
            page_note = f"Page {page + 1}/{pages} — " if pages > 1 else ""
            embed.set_footer(
                text=f"{page_note}{n} song{'s' if n != 1 else ''} queued — "
                     f"about {fmt_duration(total)} remaining"
            )
        return embed, pages

    @commands.hybrid_command(name="queue", aliases=["q"],
                      help="Show the current queue.")
    async def show_queue(self, ctx: commands.Context):
        player = self.players.get(ctx.guild.id)
        if player is None or (player.current is None and not player.queue):
            await ctx.send("The queue is empty.")
            return
        guild_id = ctx.guild.id
        await PageView(lambda p: self._queue_embed(guild_id, p)).send(ctx)

    @commands.hybrid_command(help="Move queue slot N to the front (plays next).")
    async def move(self, ctx: commands.Context, index: int):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send("The queue is empty.")
            return
        if not 1 <= index <= len(player.queue):
            await ctx.send(f"Pick a slot between 1 and {len(player.queue)}.")
            return
        if index == 1:
            await ctx.send("That song is already next up.")
            return
        track = player.queue[index - 1]
        del player.queue[index - 1]
        player.queue.appendleft(track)
        await ctx.send(f"⏫ **{track.title}** will play next.")

    @commands.hybrid_command(help="Shuffle the queue (the playing song keeps playing).")
    async def shuffle(self, ctx: commands.Context):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is None or len(player.queue) < 2:
            await ctx.send("Need at least 2 queued songs to shuffle.")
            return
        shuffled = list(player.queue)
        random.shuffle(shuffled)
        player.queue.clear()
        player.queue.extend(shuffled)
        await ctx.send(f"🔀 Shuffled **{len(shuffled)}** queued tracks.")

    @commands.hybrid_command(help="Remove queue slot N (1 = next up). The playing song "
                           "can't be removed — skip it instead.")
    async def remove(self, ctx: commands.Context, index: int):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send("The queue is empty.")
            return
        if not 1 <= index <= len(player.queue):
            await ctx.send(f"Pick a slot between 1 and {len(player.queue)}.")
            return
        removed = player.queue[index - 1]
        del player.queue[index - 1]
        await ctx.send(f"🗑️ Removed **{removed.title}** (slot {index}).")

    @commands.hybrid_command(name="removerange",
                      brief="Remove queue slots X through Y.",
                      help="Remove queue slots X through Y — a!removerange 2 5 "
                           "and a!removerange 2-5 both work. The playing song "
                           "is never touched.")
    async def remove_range(self, ctx: commands.Context, *, span: str):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send("The queue is empty.")
            return
        m = re.fullmatch(r"(\d+)\s*(?:-|to)\s*(\d+)|(\d+)\s+(\d+)", span.strip())
        if not m:
            await ctx.send("Use it like `a!removerange 2-5` (or `a!removerange 2 5`).")
            return
        start, end = (int(g) for g in m.groups() if g is not None)
        if start > end:
            await ctx.send("The first slot must be less than or equal to the second.")
            return
        if start < 1 or end > len(player.queue):
            await ctx.send(f"Pick slots between 1 and {len(player.queue)}.")
            return
        kept = list(player.queue)
        removed = kept[start - 1:end]
        del kept[start - 1:end]
        player.queue.clear()
        player.queue.extend(kept)
        if len(removed) == 1:
            await ctx.send(f"🗑️ Removed **{removed[0].title}** (slot {start}).")
        else:
            await ctx.send(
                f"🗑️ Removed **{len(removed)}** tracks (slots {start}–{end}). "
                f"{len(player.queue)} left in the queue."
            )

    @commands.hybrid_command(help="Clear the whole queue (doesn't touch the playing song).")
    async def clear(self, ctx: commands.Context):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send("The queue is already empty.")
            return
        n = len(player.queue)
        player.queue.clear()
        await ctx.send(f"🧹 Cleared **{n}** track{'s' if n != 1 else ''} from the queue.")

    @commands.hybrid_command(name="nowplaying", aliases=["np"],
                      help="Show the current song and elapsed time.")
    async def now_playing(self, ctx: commands.Context):
        player = self.players.get(ctx.guild.id)
        if player is None or player.current is None:
            await ctx.send("Nothing is playing.")
            return
        t = player.current
        elapsed = time.monotonic() - player.started_at if player.started_at else 0
        loop_note = f"\n🔁 Looping {player.loops_left}x more" if player.loops_left else ""
        embed = discord.Embed(
            title="Now Playing",
            description=(
                f"**{t.title}**\n"
                f"`{fmt_duration(elapsed)} / {fmt_duration(t.duration)}` "
                f"— queued by {t.requester}{loop_note}"
            ),
            color=0x5865F2,
        )
        if t.webpage_url:
            embed.url = t.webpage_url
        await player.post_controls(embed=embed, sender=ctx.send)

    @commands.hybrid_command(help="Show the last 10 songs played.")
    async def history(self, ctx: commands.Context):
        past = self.histories.get(ctx.guild.id)
        if not past:
            await ctx.send("No songs have been played yet.")
            return
        lines = [
            f"`{i}.` {t.title} [{fmt_duration(t.duration)}] — {t.requester}"
            for i, t in enumerate(reversed(past), start=1)  # newest first
        ]
        embed = discord.Embed(
            title="🕘 Recently played", description="\n".join(lines),
            color=0x5865F2)
        embed.set_footer(text="Newest first. History resets when the bot restarts.")
        await ctx.send(embed=embed)

    @commands.hybrid_command(help="Stop everything and leave the voice channel.")
    async def stop(self, ctx: commands.Context):
        if not await self.ensure_voice(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player:
            player.destroy()
        elif ctx.voice_client:
            await ctx.voice_client.disconnect(force=True)
        await ctx.send("👋 Stopped and left the channel.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
