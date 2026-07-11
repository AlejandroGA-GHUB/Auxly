# Auxly 🎵

A simple Discord music bot that runs on your own machine, built around 
audio quality, ease of setup, and support for many genuinely useful commands. 

It plays YouTube links, Spotify links (tracks, albums, playlists), YouTube
playlists, plain search terms, and your own audio files.

## One-time setup

> **Windows is the supported platform.** The bot itself is plain Python and
> runs fine on Linux/macOS too, but the helper scripts (`setup.bat`,
> `auxly_start.bat`) are Windows-only: on other systems, follow *Manual
> setup* below and start the bot with `python bot.py`.

1. **Run `setup.bat`** (double-click). It installs everything the bot needs
   (Python, FFmpeg, Deno, and the Python packages), skipping anything you
   already have. It then creates your `.env` file and opens it in Notepad, ready for
   the token from step 2. If it had to install Python, it will ask you to
   run it a second time, since new installs aren't visible to an
   already-open window. It uses winget, which Windows 10/11 ships with. If
   you prefer doing things by hand, see *Manual setup* below.

2. **Create the Discord bot:**
   1. Go to https://discord.com/developers/applications → *New Application*
   2. **Bot** tab → *Reset Token* → copy the token
   3. Still on the **Bot** tab, enable **Message Content Intent** (required
      for `a!` commands)
   4. **OAuth2 → URL Generator**: check `bot` **and** `applications.commands`
      (the latter enables /slash commands), then permissions *View Channels*,
      *Send Messages*, *Embed Links*, *Read Message History*, *Connect*,
      *Speak*. Open the generated URL to invite the bot to your server.
      (Without *View Channels* the bot can't see any channel and will
      silently ignore everything. Already invited the bot without
      `applications.commands`? Just open the new URL. Re-inviting updates
      the bot in place.)

3. **Paste the token into `.env`**, the file `setup.bat` created and opened
   for you: `DISCORD_TOKEN=your-token-here`. Save and close. That's it.

   *(Optional, only for Spotify albums/playlists)*: create an app at
   https://developer.spotify.com/dashboard and fill in `SPOTIFY_CLIENT_ID`
   and `SPOTIFY_CLIENT_SECRET`. Note that since Feb 2026, Spotify requires
   the account creating the app to have **Premium** (any Premium holder can
   create it and hand you the two keys). Without keys, single Spotify
   **track** links still work; only albums and playlists need them.

<details>
<summary><b>Manual setup</b> (what <code>setup.bat</code> automates)</summary>

1. Install [Python 3.11+](https://www.python.org/downloads/) and
   [FFmpeg](https://ffmpeg.org/download.html). FFmpeg must be on your
   PATH (`ffmpeg -version` should work in a terminal).
   Also recommended: [Deno](https://deno.com), a JavaScript runtime that
   yt-dlp uses to solve YouTube's stream challenges. The bot runs without
   it, but YouTube may throttle the audio stream (playback stutter) when
   it's missing.
2. Install the Python packages:
   ```
   pip install -r requirements.txt
   ```
3. Create your config file and add the token from step 2 above:
   ```
   copy .env.example .env
   ```
</details>

## Launch

Double-click **`auxly_start.bat`**. The launcher shows which Auxly version
you're running, then quietly checks for updates to yt-dlp and its solver
scripts (takes about 2 seconds, does nothing if you're current).
YouTube changes things often and a stale yt-dlp is the most common way any
music bot breaks, so this keeps Auxly self-healing. It also shows how much
disk the bot is using (database plus stored audio files), then starts the
bot **in the background**: you can close the launcher window and the bot
keeps running, with its console output written to `auxly.log`.

The launcher window then gives you the choice:

- **ESC** closes the window and leaves Auxly running windowless. Stop it
  later with `a!shutdown` in Discord (owner only).
- **Ctrl+C** (with the window still open) shuts Auxly down right there.

Prefer a visible console? Run `python bot.py` in a terminal instead. Output
stays on screen and Ctrl+C stops the bot, but closing that terminal kills it.

## Updating

Auxly **never updates itself**. That's deliberate: the code running on your
machine only ever changes when you change it. The bot just checks the
version number, once at startup (a note in the console/log) and whenever
the owner runs `a!version`, by comparing your local `VERSION.txt` against
the latest release on GitHub. If a newer one is out, it tells you.

To update:

1. Download the new release ZIP from this repo's Releases page.
2. **Extract it straight into your existing bot folder**, overwriting when
   asked.
3. Run `setup.bat` once (usually a no-op; it catches new dependencies).

That's the whole update. Your data survives automatically because releases
never contain it: `.env` (your token), `auxly.db` (profiles and playlists),
and `audio_files/` (stored uploads) only exist in your folder, so
extracting over the top can't touch them.

If you extract to a fresh folder instead, move those three files into it
manually to keep all your data intact.

## Commands (prefix `a!`, and every command also works as a /slash command)

Type `/play`, `/skip`, `/playlist view`, and so on, and Discord shows the
parameters as fillable fields. The one exception: **attached audio files
work with `a!play` only**, since slash commands can't carry attachments
here. Slash commands may take up to an hour to first appear after the very
first launch (Discord-side propagation, one time only).

| Command | What it does |
|---|---|
| `a!play <link or search>` | Play a song, or queue it if one's already playing. YouTube/Spotify links, playlists, search terms, or an **attached audio file** (mp3, wav, flac, ogg, m4a…). |
| `a!playnext <link or search>` | Like play, but jumps the queue (plays right after the current song) |
| `a!join` | Bring the bot into your voice channel without playing anything |
| `a!pause` / `a!resume` | Pause / resume |
| `a!skip` | Skip the current song (cancels any loop) |
| `a!loop <n>` | Repeat the current song n more times; the queue waits. `a!loop 0` cancels. |
| `a!queue` | Show the queue (long queues get « First / ◀ Prev / Next ▶ / Last » page buttons) |
| `a!shuffle` | Shuffle the queue (playing song keeps playing) |
| `a!move <n>` | Move queue slot n to the front (plays next) |
| `a!save <playlist>` | Save the **currently playing** song to your playlist |
| `a!remove <n> [n ...]` | Remove queue slots by number (1 = next up); `a!remove 2 5 9` removes all three |
| `a!removerange <x> <y>` | Remove queue slots x through y (`2 5` or `2-5` both work) |
| `a!clear` | Empty the queue (keeps the playing song) |
| `a!nowplaying` | Current song + elapsed time |
| `a!history` | Last 10 songs played (resets on restart) |
| `a!help` | List all commands |
| `a!profilehelp` | Profile & playlist commands (below) |

Every "Now playing" message (automatic or from `a!nowplaying`) comes with
**Pause/Resume · Loop · Skip** buttons. Each Loop click adds one repeat
(the label counts up, "Loop (3)"), and while a loop is active a red
*Cancel Loop* button appears; `a!loop <n>` still works for big numbers.
Buttons follow the same rule as commands (you must be in the bot's voice
channel), and only the newest message keeps live buttons.

The bot auto-disconnects after **1 hour** of silence. If everyone leaves the
voice channel it pauses immediately, waits **5 minutes** for someone to come
back (resuming where it left off), then leaves.

## Profiles & saved playlists

Anyone can create a profile (one per Discord account) and save named
playlists on it: song collections that persist between bot restarts and can
be queued anytime, on any server the bot is in.

| Command | What it does |
|---|---|
| `a!profile create <name>` | Create your profile |
| `a!profile rename <newname>` | Rename your profile |
| `a!profiles` | List all profiles |
| `a!playlist create <name>` | New playlist on your profile |
| `a!playlist add <name> <song>` | Add anything `a!play` accepts: links, searches, whole YouTube/Spotify playlists (expanded into songs). Attached files can't be added this way since their Discord links expire; see `addfile`. |
| `a!playlist addfile <name>` | Store attached audio files in your playlist (needs permission from the bot owner, see below; `a!` prefix only) |
| `a!playlist view` | List your playlists |
| `a!playlist view <profile>` | List someone's playlists |
| `a!playlist view [profile] <name>` | Show a playlist's songs (long lists get « First / ◀ Prev / Next ▶ / Last » page buttons) |
| `a!playlist playall [profile] <name> [y]` | Queue a whole playlist. Add `y` at the end to shuffle it in (optional). |
| `a!playlist play [profile] <name> <n> <n> ...` | Queue just those numbered songs from a playlist (numbers from `a!playlist view`), in the order you list them |
| `a!playlist move <name> <from> <to>` | Move a song to another slot |
| `a!playlist swap <name> <x> <y>` | Swap two songs' slots |
| `a!playlist remove <name> <n> [n ...]` | Remove songs by number — `a!playlist remove ost 3 9 14` removes all three |
| `a!playlist removerange <name> <x> <y>` | Remove songs x through y (`2 5` or `2-5` both work) |
| `a!playlist rename <old> <new>` | Rename your playlist |
| `a!playlist clear <name>` | Remove all songs from your playlist (keeps the playlist) |
| `a!playlist delete <name>` | Delete your playlist |

Details:

- Profiles are **global**: keyed by your Discord account, so your playlists
  follow you to every server the bot is in.
- Anyone can view and queue anyone's playlist; only you can change yours.
- Profile names are one word; **playlist names can contain spaces**
  (`This Playlist`) and never need quotes. Commands figure out where the
  name ends on their own. All names are case-insensitive, max 32 characters.
- Each profile holds at most **1,000 songs** total across all its playlists
  (an anti-abuse guard). Oversized adds fill up to the cap and tell you how
  many were skipped. The bot owner can change this limit with
  `PROFILE_SONG_CAP` in `.env`.
- A playlist never holds the same song twice; duplicate adds are skipped
  with a note. The same song can still live in as many *different* playlists
  as you like. (Matching is by link, so the same track added via two
  different links can occasionally slip through.)
- Everything is stored in a small local SQLite file, `auxly.db`, created
  automatically next to `bot.py`. Songs are stored as references (title plus
  link, about 200 bytes each), never audio, so even thousands of saved songs
  use under a megabyte. Delete the file to wipe all profiles.
- **Stored audio files** are the one exception: users the bot owner trusts
  (granted with `a!grantfiles @user`, see `a!devhelp`) can attach audio
  files to `a!playlist addfile <name>` and keep them permanently. Files land
  in an `audio_files/` folder next to the bot, capped at **25 MB per file**
  and **100 files per profile** (inside the song cap) since this is real
  audio on your disk; the owner can change the file count with
  `PROFILE_FILE_CAP` in `.env`. They mix freely with normal songs in a
  playlist
  (marked 📁 in `a!playlist view`), never expire, and are deleted from disk
  when their song, playlist, or profile is removed. Revoking permission
  stops new uploads but keeps existing files playable.
## Owner commands

These only work for the bot owner (the Discord account the bot's application
belongs to), are prefix-only (`a!`, no slash versions, so they never show up
in the / picker), and are kept out of the regular help on purpose. In
Discord, `a!devhelp` lists them all.

| Command | What it does |
|---|---|
| `a!devhelp` | List all owner commands |
| `a!shutdown` | Cleanly shut the bot down (the way to stop a windowless launch) |
| `a!stop` | Stop playback and leave the voice channel |
| `a!profile delete <name>` | Delete any user's profile and all its playlists (frees their stored files too) |
| `a!revokepause @user` / `a!grantpause @user` | Block a user from pausing/resuming (commands and buttons), or re-allow it |
| `a!revokeclear @user` / `a!grantclear @user` | Block a user from clearing the queue, or re-allow it |
| `a!grantfiles @user` | Let a user store audio files with `a!playlist addfile` |
| `a!revokefiles @user` | Stop a user's new uploads (their existing files stay playable) |
| `a!fileperms` | List everyone with file storage permission |
| `a!storage` | Disk usage: database size, stored audio files, and who they belong to |
| `a!status` | Health check: uptime, version, yt-dlp version, what's playing where |
| `a!log [n]` | Show the last n lines of `auxly.log` (default 20) |
| `a!version` | Show the running version and whether a newer release is out |

## Audio quality

- yt-dlp always requests YouTube's **best audio stream, preferring Opus**
  (YouTube's highest-quality codec, ~160 kbps).
- When the source is already Opus, the bot **stream-copies it to Discord
  with zero re-encoding**: bit-for-bit the best audio YouTube serves.
- No volume filters, normalization, or effects ever touch the stream.
- Attached/linked audio files (mp3, flac, wav, …) get exactly **one** clean
  Opus encode at 510 kbps, with discord.py's default packet-loss padding
  disabled (it would otherwise audibly degrade music).
- Spotify links are resolved to song metadata and matched on YouTube.
  Spotify doesn't allow bots to stream its audio; every music bot works this
  way. Single track links work even without API keys (the bot reads the
  title from Spotify's public page metadata); albums and playlists need the
  keys.

## Troubleshooting

- **Bot joins but no sound** → make sure FFmpeg is on PATH and restart the
  bot.
- **`a!` commands ignored** → enable *Message Content Intent* (setup step
  2.3).
- **Spotify albums/playlists fail** → fill in the Spotify credentials in
  `.env` (single track links work without them).
- **A song skips the moment it starts** → usually a YouTube stream hiccup.
  The bot retries once with a fresh URL automatically and only skips (with
  a ⚠️ message) if the retry also fails. Just queue it again.
- **A specific video fails** → update yt-dlp: `pip install -U yt-dlp`.
  YouTube changes things and yt-dlp updates frequently. `auxly_start.bat`
  already does this at every launch, so a restart usually fixes it.
- **YouTube songs stutter or drift in speed while local files play fine** →
  YouTube is throttling the stream, which happens when yt-dlp can't solve
  YouTube's stream challenges. Make sure Deno is installed (`deno --version`
  in a terminal; re-run `setup.bat` to get it) and restart the bot.
- **Something else** → open an issue on this repo with what you ran and
  what happened.
