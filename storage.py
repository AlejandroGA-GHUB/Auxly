"""SQLite persistence for profiles and saved playlists.

One file (auxly.db, next to bot.py), created automatically. Profiles are keyed
by Discord user ID, so they are global: the same profile follows a user across
every server the bot is in. Songs are stored as references only (title +
source URL/search, ~200 bytes each) — except stored audio files, an
owner-granted perk: those live on disk in audio_files/ and their song rows
point at them by relative path (stored_file = 1).

All functions here are synchronous; the cog calls them via asyncio.to_thread
so the audio path is never blocked. A single shared connection + lock is
plenty at this scale.
"""

import os
import sqlite3
import threading

from dotenv import load_dotenv

# This module is imported before bot.py gets to its own load_dotenv() call,
# so the cap overrides below must load .env themselves (idempotent, cheap).
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auxly.db")


def _env_cap(name: str, default: int) -> int:
    """Optional .env override for a cap. Missing, blank, non-numeric, or
    zero/negative values all fall back to the default."""
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value >= 1 else default


# Total songs across ALL of a profile's playlists (abuse guard, ~200 KB max
# at the default). Owners can override in .env: PROFILE_SONG_CAP=5000
PROFILE_SONG_CAP = _env_cap("PROFILE_SONG_CAP", 1000)

# Stored audio files per profile (owner-granted perk; these are actual audio
# on disk, so they get their own much smaller cap inside the song cap).
# Owners can override in .env: PROFILE_FILE_CAP=250
PROFILE_FILE_CAP = _env_cap("PROFILE_FILE_CAP", 100)

# Uploaded files live here; song rows reference them by relative path
# ("audio_files/<name>") so the bot folder can be moved without breaking them.
FILE_PREFIX = "audio_files/"
FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_files")


def is_stored_file(source: str) -> bool:
    """True for songs living in audio_files/ — matches both the relative form
    stored in the DB and the absolute form tracks carry at play time."""
    return source.startswith(FILE_PREFIX) or source.startswith(FILES_DIR)


def stored_file_abs(source: str) -> str:
    """Absolute disk path for a stored-file source."""
    if source.startswith(FILE_PREFIX):
        return os.path.join(FILES_DIR, source[len(FILE_PREFIX):])
    return source


class StorageError(Exception):
    """User-facing storage error (message is safe to show in Discord)."""


_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS playlists (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    name    TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    duration    INTEGER,
    direct      INTEGER NOT NULL DEFAULT 0,
    stored_file INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_perms (
    user_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS revoked_perms (
    user_id INTEGER NOT NULL,
    action  TEXT NOT NULL,
    PRIMARY KEY (user_id, action)
);
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        # Databases created before file storage lack the stored_file column.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(songs)")]
        if "stored_file" not in cols:
            conn.execute(
                "ALTER TABLE songs ADD COLUMN stored_file "
                "INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        _conn = conn
    return _conn


def _require_profile(cur: sqlite3.Cursor, user_id: int) -> str:
    row = cur.execute(
        "SELECT name FROM profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise StorageError(
            "You don't have a profile yet — create one with "
            "`a!profile create <name>`."
        )
    return row[0]


def _playlist_id(cur: sqlite3.Cursor, user_id: int, name: str) -> int:
    row = cur.execute(
        "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
        (user_id, name),
    ).fetchone()
    if row is None:
        owner = _require_profile(cur, user_id)
        raise StorageError(f"**{owner}** has no playlist named **{name}**.")
    return row[0]


# -- profiles ---------------------------------------------------------------


def get_profile(user_id: int) -> str | None:
    with _lock:
        row = _db().execute(
            "SELECT name FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def find_profile(name: str) -> tuple[int, str] | None:
    """Look a profile up by name (case-insensitive). Returns (user_id, name)."""
    with _lock:
        row = _db().execute(
            "SELECT user_id, name FROM profiles WHERE name = ?", (name,)
        ).fetchone()
    return (row[0], row[1]) if row else None


def create_profile(user_id: int, name: str) -> None:
    with _lock:
        conn = _db()
        existing = conn.execute(
            "SELECT name FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing:
            raise StorageError(
                f"You already have a profile: **{existing[0]}**. "
                f"Rename it with `a!profile rename <newname>`."
            )
        try:
            conn.execute(
                "INSERT INTO profiles (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise StorageError(f"The name **{name}** is already taken.") from None


def rename_profile(user_id: int, new_name: str) -> str:
    """Returns the old name."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        old = _require_profile(cur, user_id)
        try:
            cur.execute(
                "UPDATE profiles SET name = ? WHERE user_id = ?",
                (new_name, user_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise StorageError(
                f"The name **{new_name}** is already taken."
            ) from None
    return old


def delete_profile(name: str) -> tuple[str, list[str]]:
    """Delete a profile (and all its playlists/songs) by name. Owner-only —
    enforced by the command, not here. Returns the exact stored name and the
    stored-file sources whose disk files the caller must delete."""
    with _lock:
        conn = _db()
        row = conn.execute(
            "SELECT user_id, name FROM profiles WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise StorageError(f"No profile named **{name}**.")
        files = [r[0] for r in conn.execute(
            "SELECT s.source FROM songs s JOIN playlists l "
            "ON s.playlist_id = l.id WHERE l.user_id = ? AND s.stored_file = 1",
            (row[0],),
        )]
        conn.execute("DELETE FROM profiles WHERE user_id = ?", (row[0],))
        conn.commit()
    return row[1], files


def list_profiles() -> list[tuple[str, int]]:
    """All profiles as (name, playlist_count)."""
    with _lock:
        rows = _db().execute(
            "SELECT p.name, COUNT(l.id) FROM profiles p "
            "LEFT JOIN playlists l ON l.user_id = p.user_id "
            "GROUP BY p.user_id ORDER BY p.name COLLATE NOCASE"
        ).fetchall()
    return rows


# -- playlists ----------------------------------------------------------------


def create_playlist(user_id: int, name: str) -> None:
    with _lock:
        conn = _db()
        cur = conn.cursor()
        _require_profile(cur, user_id)
        try:
            cur.execute(
                "INSERT INTO playlists (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise StorageError(
                f"You already have a playlist named **{name}**."
            ) from None


def rename_playlist(user_id: int, old: str, new: str) -> None:
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, old)
        try:
            cur.execute("UPDATE playlists SET name = ? WHERE id = ?", (new, pid))
            conn.commit()
        except sqlite3.IntegrityError:
            raise StorageError(
                f"You already have a playlist named **{new}**."
            ) from None


def delete_playlist(user_id: int, name: str) -> list[str]:
    """Returns the stored-file sources whose disk files the caller must
    delete."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, name)
        files = [r[0] for r in cur.execute(
            "SELECT source FROM songs WHERE playlist_id = ? AND stored_file = 1",
            (pid,),
        )]
        cur.execute("DELETE FROM playlists WHERE id = ?", (pid,))
        conn.commit()
    return files


def list_playlists(user_id: int) -> list[tuple[str, int]]:
    """A profile's playlists as (name, song_count)."""
    with _lock:
        cur = _db().cursor()
        _require_profile(cur, user_id)
        rows = cur.execute(
            "SELECT l.name, COUNT(s.id) FROM playlists l "
            "LEFT JOIN songs s ON s.playlist_id = l.id "
            "WHERE l.user_id = ? GROUP BY l.id ORDER BY l.name COLLATE NOCASE",
            (user_id,),
        ).fetchall()
    return rows


# -- songs --------------------------------------------------------------------


def add_songs(
    user_id: int, playlist: str, songs: list[tuple[str, str, int | None, bool]]
) -> tuple[int, int]:
    """songs: (title, source, duration, direct). Returns (added, dupes):
    songs whose source is already in this playlist — or repeated within the
    batch — are skipped (the same song in *other* playlists is fine), and
    added may be further clamped by the profile-wide song cap."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        seen = {r[0] for r in cur.execute(
            "SELECT source FROM songs WHERE playlist_id = ?", (pid,)
        )}
        unique = []
        for song in songs:
            if song[1] not in seen:
                seen.add(song[1])
                unique.append(song)
        dupes = len(songs) - len(unique)
        songs = unique
        if not songs:
            return 0, dupes
        total = cur.execute(
            "SELECT COUNT(*) FROM songs s JOIN playlists l "
            "ON s.playlist_id = l.id WHERE l.user_id = ?",
            (user_id,),
        ).fetchone()[0]
        room = PROFILE_SONG_CAP - total
        if room <= 0:
            raise StorageError(
                f"Your profile is at the {PROFILE_SONG_CAP:,}-song cap — "
                "remove some songs before adding more."
            )
        songs = songs[:room]
        pos = cur.execute(
            "SELECT COALESCE(MAX(position), 0) FROM songs WHERE playlist_id = ?",
            (pid,),
        ).fetchone()[0]
        cur.executemany(
            "INSERT INTO songs (playlist_id, title, source, duration, direct, "
            "position) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (pid, title, source, duration, int(direct), pos + i)
                for i, (title, source, duration, direct) in enumerate(songs, 1)
            ],
        )
        conn.commit()
    return len(songs), dupes


def get_songs(
    user_id: int, playlist: str
) -> list[tuple[str, str, int | None, bool, bool]]:
    """A playlist's songs, in order, as
    (title, source, duration, direct, stored_file)."""
    with _lock:
        cur = _db().cursor()
        pid = _playlist_id(cur, user_id, playlist)
        rows = cur.execute(
            "SELECT title, source, duration, direct, stored_file FROM songs "
            "WHERE playlist_id = ? ORDER BY position",
            (pid,),
        ).fetchall()
    return [(t, s, d, bool(direct), bool(sf)) for t, s, d, direct, sf in rows]


def clear_songs(user_id: int, playlist: str) -> tuple[int, list[str]]:
    """Remove every song from a playlist (the playlist itself stays).
    Returns (how many were removed, stored-file sources whose disk files the
    caller must delete)."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        files = [r[0] for r in cur.execute(
            "SELECT source FROM songs WHERE playlist_id = ? AND stored_file = 1",
            (pid,),
        )]
        cur.execute("DELETE FROM songs WHERE playlist_id = ?", (pid,))
        removed = cur.rowcount
        conn.commit()
    return removed, files


def _song_ids(cur: sqlite3.Cursor, pid: int) -> list[int]:
    return [r[0] for r in cur.execute(
        "SELECT id FROM songs WHERE playlist_id = ? ORDER BY position", (pid,)
    ).fetchall()]


def _check_slots(cur: sqlite3.Cursor, playlist: str, count: int, *slots: int):
    for s in slots:
        if not 1 <= s <= count:
            raise StorageError(
                f"**{playlist}** has {count} song{'s' if count != 1 else ''} — "
                f"pick slots between 1 and {count}."
            )


def move_song(user_id: int, playlist: str, frm: int, to: int) -> str:
    """Move the frm-th song to slot to (1-based). Returns its title."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        ids = _song_ids(cur, pid)
        _check_slots(cur, playlist, len(ids), frm, to)
        moved = ids.pop(frm - 1)
        ids.insert(to - 1, moved)
        cur.executemany(
            "UPDATE songs SET position = ? WHERE id = ?",
            [(pos, sid) for pos, sid in enumerate(ids, 1)],
        )
        title = cur.execute(
            "SELECT title FROM songs WHERE id = ?", (moved,)
        ).fetchone()[0]
        conn.commit()
    return title


def swap_songs(user_id: int, playlist: str, a: int, b: int) -> tuple[str, str]:
    """Swap the a-th and b-th songs (1-based). Returns their titles."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        ids = _song_ids(cur, pid)
        _check_slots(cur, playlist, len(ids), a, b)
        ids[a - 1], ids[b - 1] = ids[b - 1], ids[a - 1]
        cur.executemany(
            "UPDATE songs SET position = ? WHERE id = ?",
            [(pos, sid) for pos, sid in enumerate(ids, 1)],
        )
        titles = tuple(
            cur.execute("SELECT title FROM songs WHERE id = ?", (sid,)).fetchone()[0]
            for sid in (ids[b - 1], ids[a - 1])
        )
        conn.commit()
    return titles


def remove_songs(user_id: int, playlist: str, indexes: list[int]
                 ) -> tuple[list[int], list[str], list[str]]:
    """Remove songs by 1-based slot — duplicates deduped, out-of-range slots
    skipped. Returns (slots actually removed ascending, their titles,
    stored-file sources whose disk files the caller must delete)."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        rows = cur.execute(
            "SELECT id, title, source, stored_file FROM songs "
            "WHERE playlist_id = ? ORDER BY position", (pid,)
        ).fetchall()
        valid = [i for i in sorted(set(indexes)) if 1 <= i <= len(rows)]
        if not valid:
            raise StorageError(
                f"**{playlist}** has {len(rows)} "
                f"song{'s' if len(rows) != 1 else ''} — pick a valid slot."
            )
        picked = [rows[i - 1] for i in valid]
        cur.executemany("DELETE FROM songs WHERE id = ?",
                        [(r[0],) for r in picked])
        conn.commit()
    return valid, [r[1] for r in picked], [r[2] for r in picked if r[3]]


def remove_song_range(user_id: int, playlist: str, start: int, end: int
                      ) -> tuple[list[str], list[str]]:
    """Remove songs start..end (1-based, inclusive). Returns (removed titles,
    stored-file sources whose disk files the caller must delete)."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        rows = cur.execute(
            "SELECT id, title, source, stored_file FROM songs "
            "WHERE playlist_id = ? ORDER BY position", (pid,)
        ).fetchall()
        _check_slots(cur, playlist, len(rows), start, end)
        picked = rows[start - 1:end]
        cur.executemany("DELETE FROM songs WHERE id = ?",
                        [(r[0],) for r in picked])
        conn.commit()
    return [r[1] for r in picked], [r[2] for r in picked if r[3]]


# -- stored audio files -------------------------------------------------------


def _file_counts(cur: sqlite3.Cursor, user_id: int) -> tuple[int, int]:
    """(total songs, stored files) across all of a profile's playlists."""
    return cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(s.stored_file), 0) FROM songs s "
        "JOIN playlists l ON s.playlist_id = l.id WHERE l.user_id = ?",
        (user_id,),
    ).fetchone()


def file_room(user_id: int, playlist: str) -> int:
    """How many more files this profile can store right now (also validates
    that the playlist exists). Raises with the binding cap's message if none."""
    with _lock:
        cur = _db().cursor()
        _playlist_id(cur, user_id, playlist)
        total, files = _file_counts(cur, user_id)
    if files >= PROFILE_FILE_CAP:
        raise StorageError(
            f"Your profile is at the {PROFILE_FILE_CAP}-stored-file cap — "
            "remove some files before adding more."
        )
    if total >= PROFILE_SONG_CAP:
        raise StorageError(
            f"Your profile is at the {PROFILE_SONG_CAP:,}-song cap — "
            "remove some songs before adding more."
        )
    return min(PROFILE_SONG_CAP - total, PROFILE_FILE_CAP - files)


def add_file_song(
    user_id: int, playlist: str, title: str, source: str,
    duration: int | None = None,
) -> None:
    """Add one stored-file song (source is its audio_files/ relative path).
    Re-checks both caps so the guard holds even if room changed since
    file_room() was consulted."""
    with _lock:
        conn = _db()
        cur = conn.cursor()
        pid = _playlist_id(cur, user_id, playlist)
        total, files = _file_counts(cur, user_id)
        if files >= PROFILE_FILE_CAP:
            raise StorageError(
                f"Your profile is at the {PROFILE_FILE_CAP}-stored-file cap — "
                "remove some files before adding more."
            )
        if total >= PROFILE_SONG_CAP:
            raise StorageError(
                f"Your profile is at the {PROFILE_SONG_CAP:,}-song cap — "
                "remove some songs before adding more."
            )
        pos = cur.execute(
            "SELECT COALESCE(MAX(position), 0) FROM songs WHERE playlist_id = ?",
            (pid,),
        ).fetchone()[0]
        cur.execute(
            "INSERT INTO songs (playlist_id, title, source, duration, direct, "
            "stored_file, position) VALUES (?, ?, ?, ?, 1, 1, ?)",
            (pid, title, source, duration, pos + 1),
        )
        conn.commit()


def storage_report() -> tuple[int, int, int, list[tuple[str, int, int, int]]]:
    """Disk usage snapshot for a!storage. Returns (db_bytes, stored file
    count, stored files' total bytes, per-profile rows). The totals come from
    scanning audio_files/ itself (ground truth, catches orphans); each row is
    (profile name, total songs, stored files, those files' bytes on disk),
    only for profiles that have stored files."""
    db_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    with _lock:
        conn = _db()
        counts = conn.execute(
            "SELECT p.name, COUNT(s.id), COALESCE(SUM(s.stored_file), 0) "
            "FROM profiles p "
            "LEFT JOIN playlists l ON l.user_id = p.user_id "
            "LEFT JOIN songs s ON s.playlist_id = l.id "
            "GROUP BY p.user_id ORDER BY p.name COLLATE NOCASE"
        ).fetchall()
        stored = conn.execute(
            "SELECT p.name, s.source FROM songs s "
            "JOIN playlists l ON s.playlist_id = l.id "
            "JOIN profiles p ON p.user_id = l.user_id "
            "WHERE s.stored_file = 1"
        ).fetchall()
    per_profile: dict[str, int] = {}
    for name, source in stored:
        try:
            size = os.path.getsize(stored_file_abs(source))
        except OSError:
            size = 0  # row survives a missing file; report it as 0 bytes
        per_profile[name] = per_profile.get(name, 0) + size
    total_files = total_bytes = 0
    if os.path.isdir(FILES_DIR):
        for entry in os.scandir(FILES_DIR):
            if entry.is_file():
                total_files += 1
                total_bytes += entry.stat().st_size
    rows = [(name, songs, files, per_profile.get(name, 0))
            for name, songs, files in counts if files]
    return db_bytes, total_files, total_bytes, rows


# -- file-storage permissions -------------------------------------------------


def has_file_perm(user_id: int) -> bool:
    with _lock:
        row = _db().execute(
            "SELECT 1 FROM file_perms WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def set_file_perm(user_id: int, allowed: bool) -> bool:
    """Grant or revoke file storage. Returns True if anything changed."""
    with _lock:
        conn = _db()
        if allowed:
            cur = conn.execute(
                "INSERT OR IGNORE INTO file_perms (user_id) VALUES (?)",
                (user_id,),
            )
        else:
            cur = conn.execute(
                "DELETE FROM file_perms WHERE user_id = ?", (user_id,)
            )
        conn.commit()
    return cur.rowcount > 0


def list_file_perms() -> list[int]:
    with _lock:
        rows = _db().execute("SELECT user_id FROM file_perms").fetchall()
    return [r[0] for r in rows]


def is_revoked(user_id: int, action: str) -> bool:
    """True if the owner has revoked this user's access to an action
    ("pause" covers pause+resume, "clear" is queue-clearing)."""
    with _lock:
        row = _db().execute(
            "SELECT 1 FROM revoked_perms WHERE user_id = ? AND action = ?",
            (user_id, action),
        ).fetchone()
    return row is not None


def set_revoked(user_id: int, action: str, revoked: bool) -> bool:
    """Revoke or restore an action for a user. Returns True if anything
    changed."""
    with _lock:
        conn = _db()
        if revoked:
            cur = conn.execute(
                "INSERT OR IGNORE INTO revoked_perms (user_id, action) "
                "VALUES (?, ?)",
                (user_id, action),
            )
        else:
            cur = conn.execute(
                "DELETE FROM revoked_perms WHERE user_id = ? AND action = ?",
                (user_id, action),
            )
        conn.commit()
    return cur.rowcount > 0


def list_revoked() -> list[tuple[int, str]]:
    """All (user_id, action) revocations, grouped by action."""
    with _lock:
        rows = _db().execute(
            "SELECT user_id, action FROM revoked_perms ORDER BY action"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]
