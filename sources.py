"""Track resolution: yt-dlp extraction and Spotify -> YouTube search mapping.

Audio quality note: we always ask yt-dlp for bestaudio, preferring Opus so the
player can stream-copy the codec (zero re-encode) straight into Discord.
"""

import asyncio
import concurrent.futures
import functools
import html as html_mod
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import yt_dlp

# Prefer Opus (YouTube's highest-quality audio codec) so playback can copy the
# stream without re-encoding. Fall back to whatever bestaudio is available.
YTDL_FORMAT = "bestaudio[acodec=opus]/bestaudio/best"

_BASE_OPTS = {
    "format": YTDL_FORMAT,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # bind IPv4; avoids some throttling issues
}

# Direct audio files (Discord attachments or plain links): FFmpeg plays these
# itself, no yt-dlp involved.
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma")

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
)
YOUTUBE_PLAYLIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass
class Track:
    """One queued song. `source` is a URL or a ytsearch query; the actual
    stream URL is resolved lazily at play time so it can never expire."""

    title: str
    source: str
    duration: int | None = None  # seconds, if known
    requester: str = ""
    requester_id: int = 0  # Discord user id; majority skip exempts the requester
    webpage_url: str | None = None
    direct: bool = False  # audio file URL: FFmpeg plays it as-is, no yt-dlp


class TrackError(Exception):
    """User-facing resolution error."""


class _ExtractionFailed(Exception):
    """Picklable stand-in for yt-dlp errors crossing the process boundary."""


# yt-dlp extraction is CPU-heavy Python code. Run it in a separate PROCESS,
# not a thread: a thread shares the GIL with discord.py's audio-sender thread
# and starves it, which is audible as a slowdown + speed-up-to-catch-up burst.
_extract_pool: concurrent.futures.ProcessPoolExecutor | None = None


def _get_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _extract_pool
    if _extract_pool is None:
        _extract_pool = concurrent.futures.ProcessPoolExecutor(max_workers=2)
    return _extract_pool


def _warm_worker() -> bool:
    """No-op ran in a fresh worker: forces it to spawn and import yt-dlp
    (workers import this module) so neither ever happens mid-playback."""
    return True


def prewarm():
    """Spin up both extraction workers at startup instead of lazily at the
    first play — first play gets faster, and no process spawn (a brief CPU
    spike) can coincide with live audio."""
    pool = _get_pool()
    for _ in range(2):
        pool.submit(_warm_worker)


def _extract_sync(query: str, opts: dict) -> dict | None:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise _ExtractionFailed(str(e)) from None


async def _extract(query: str, **extra_opts) -> dict:
    opts = {**_BASE_OPTS, **extra_opts}
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            _get_pool(), functools.partial(_extract_sync, query, opts)
        )
    except _ExtractionFailed as e:
        raise TrackError(f"Couldn't fetch that: {e}") from e
    if data is None:
        raise TrackError("Nothing found for that link/search.")
    return data


def is_audio_file_url(url: str) -> bool:
    if not URL_RE.match(url):
        return False
    path = url.split("?", 1)[0].split("#", 1)[0]
    return path.lower().endswith(AUDIO_EXTS)


async def get_stream_url(track: Track) -> str:
    """Resolve a fresh bestaudio stream URL right before playback."""
    if track.direct:
        return track.source
    data = await _extract(track.source, noplaylist=True)
    if "entries" in data:  # search result
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise TrackError(f"No YouTube match found for: {track.title}")
        data = entries[0]
    # Fill in metadata we may not have had (e.g. Spotify-sourced tracks)
    track.duration = track.duration or data.get("duration")
    track.webpage_url = data.get("webpage_url", track.webpage_url)
    if not track.title or track.source == track.title:
        track.title = data.get("title", track.title)
    url = data.get("url")
    if not url:
        raise TrackError(f"No playable audio stream for: {track.title}")
    return url


async def resolve(query: str, requester: str) -> list[Track]:
    """Turn user input (URL, search terms, playlist link) into Track(s)."""
    query = query.strip("<>").strip()

    if is_audio_file_url(query):
        name = query.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        return [Track(title=name, source=query, requester=requester, direct=True)]

    if SPOTIFY_URL_RE.search(query):
        return await _resolve_spotify(query, requester)

    if URL_RE.match(query) and YOUTUBE_PLAYLIST_RE.search(query):
        return await _resolve_youtube_playlist(query, requester)

    # Single URL or plain search terms.
    data = await _extract(query, noplaylist=True)
    if "entries" in data:  # search returned a list
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise TrackError("No results found.")
        data = entries[0]
    return [
        Track(
            title=data.get("title", query),
            source=data.get("webpage_url", query),
            duration=data.get("duration"),
            requester=requester,
            webpage_url=data.get("webpage_url"),
        )
    ]


async def _resolve_youtube_playlist(url: str, requester: str) -> list[Track]:
    # Flat extraction: fast, titles + video URLs only. Streams resolve at play time.
    data = await _extract(url, extract_flat="in_playlist")
    entries = [e for e in data.get("entries", []) if e]
    if not entries:
        # A watch URL with a &list= param can also be a single video; fall back.
        return await resolve(data.get("webpage_url", url), requester)
    return [
        Track(
            title=e.get("title") or "Unknown",
            source=e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            duration=e.get("duration"),
            requester=requester,
        )
        for e in entries
    ]


# ---------------------------------------------------------------------------
# Spotify: metadata only (API), audio comes from a YouTube search match.
# ---------------------------------------------------------------------------

_spotify_client = None

_BROWSER_UA = {"User-Agent": "Mozilla/5.0"}


def _keyless_track_title(url: str) -> str:
    """No-credentials fallback for single track links: read the song's title
    and artist from Spotify's public page metadata. Metadata only — audio
    still comes from the usual YouTube search match.

    The track page's <title> ("Song - song and lyrics by Artist | Spotify")
    gives us the artist too, so the YouTube match stays precise; the official
    oEmbed endpoint (title only) is the fallback if that format ever changes.
    """
    try:
        req = urllib.request.Request(url, headers=_BROWSER_UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read(262144).decode("utf-8", "replace")
        m = re.search(r"<title>(.*?)\s*\|\s*Spotify</title>", page, re.S)
        if m:
            text = html_mod.unescape(m.group(1)).strip()
            m2 = re.match(r"(.+?) - (?:song(?: and lyrics)?|single) by (.+)", text)
            if m2:
                return f"{m2.group(2)} - {m2.group(1)}"
            if text:
                return text
    except Exception:
        pass  # fall through to oEmbed
    query = urllib.parse.urlencode({"url": url})
    req = urllib.request.Request(
        f"https://open.spotify.com/oembed?{query}", headers=_BROWSER_UA
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["title"]


def _get_spotify():
    global _spotify_client
    if _spotify_client is None:
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not client_id or not secret:
            raise TrackError(
                "Spotify links need API credentials. Add SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET to the .env file (see README)."
            )
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials

        _spotify_client = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=client_id, client_secret=secret
            )
        )
    return _spotify_client


def _spotify_track_to_query(t: dict) -> tuple[str, int]:
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    title = f"{artists} - {t['name']}" if artists else t["name"]
    return title, (t.get("duration_ms") or 0) // 1000


async def _resolve_spotify(url: str, requester: str) -> list[Track]:
    kind, sid = SPOTIFY_URL_RE.search(url).groups()
    loop = asyncio.get_running_loop()

    if not (os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET")):
        if kind != "track":
            raise TrackError(
                "Spotify albums/playlists need API credentials — add "
                "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to the .env file "
                "(see README). Single Spotify **track** links work without them."
            )
        try:
            title = await loop.run_in_executor(None, _keyless_track_title, url)
        except Exception as e:
            raise TrackError(f"Spotify lookup failed: {e}") from e
        return [
            Track(
                title=title,
                source=f"ytsearch1:{title} audio",
                requester=requester,
            )
        ]

    sp = _get_spotify()

    def fetch() -> list[tuple[str, int]]:
        if kind == "track":
            return [_spotify_track_to_query(sp.track(sid))]
        if kind == "album":
            items = []
            page = sp.album_tracks(sid)
            while page:
                items += [_spotify_track_to_query(t) for t in page["items"]]
                page = sp.next(page) if page.get("next") else None
            return items
        # playlist
        items = []
        page = sp.playlist_items(sid, additional_types=("track",))
        while page:
            for it in page["items"]:
                t = it.get("track")
                if t and t.get("name"):  # skip local/unavailable tracks
                    items.append(_spotify_track_to_query(t))
            page = sp.next(page) if page.get("next") else None
        return items

    try:
        results = await loop.run_in_executor(None, fetch)
    except TrackError:
        raise
    except Exception as e:
        raise TrackError(f"Spotify lookup failed: {e}") from e

    if not results:
        raise TrackError("That Spotify link has no playable tracks.")

    # Each track becomes a YouTube search, resolved lazily at play time.
    return [
        Track(
            title=title,
            source=f"ytsearch1:{title} audio",
            duration=dur or None,
            requester=requester,
        )
        for title, dur in results
    ]
