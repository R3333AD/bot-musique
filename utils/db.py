import json
import time

import aiosqlite
from config import DATABASE_PATH


async def _connect(db):
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        try:
            await db.execute("ALTER TABLE guild_settings ADD COLUMN theme_color TEXT DEFAULT '#6C5CE7'")
        except Exception:
            pass
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                uri TEXT NOT NULL,
                author TEXT DEFAULT '',
                duration INTEGER DEFAULT 0,
                position INTEGER DEFAULT 0,
                FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                theme_color TEXT DEFAULT '#6C5CE7'
            );
            CREATE TABLE IF NOT EXISTS spotify_cache (
                cache_key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queue_state (
                guild_id INTEGER PRIMARY KEY,
                track_data TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                loop_mode TEXT,
                saved_at TEXT DEFAULT (datetime('now'))
            );
        """)


async def save_playlist(user_id: int, name: str, tracks: list[dict]):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        await db.execute("DELETE FROM playlists WHERE user_id = ? AND name = ?", (user_id, name))
        cursor = await db.execute(
            "INSERT INTO playlists (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
        playlist_id = cursor.lastrowid
        for i, t in enumerate(tracks):
            await db.execute(
                "INSERT INTO playlist_tracks (playlist_id, title, uri, author, duration, position) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (playlist_id, t["title"], t["uri"], t["author"], t.get("duration", 0), i),
            )
        await db.commit()
        return playlist_id


async def load_playlist(user_id: int, name: str) -> list[dict] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        cursor = await db.execute(
            "SELECT title, uri, author, duration FROM playlist_tracks "
            "WHERE playlist_id = ? ORDER BY position",
            (row["id"],),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def list_playlists(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT name, created_at, (SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = playlists.id) AS track_count "
            "FROM playlists WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def set_music_channel(guild_id: int, channel_id: int | None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        if channel_id is None:
            await db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
        else:
            await db.execute(
                "INSERT INTO guild_settings (guild_id, channel_id) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = ?",
                (guild_id, channel_id, channel_id),
            )
        await db.commit()


async def get_music_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        cursor = await db.execute(
            "SELECT channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_guild_theme(guild_id: int, color_hex: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        await db.execute(
            "INSERT INTO guild_settings (guild_id, theme_color) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET theme_color = ?",
            (guild_id, color_hex, color_hex),
        )
        await db.commit()


async def get_guild_theme(guild_id: int) -> str:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        cursor = await db.execute(
            "SELECT theme_color FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else "#6C5CE7"


async def append_to_playlist(user_id: int, name: str, tracks: list[dict]):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        cursor = await db.execute(
            "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()
        if row:
            playlist_id = row[0]
            cursor = await db.execute(
                "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks WHERE playlist_id = ?",
                (playlist_id,),
            )
            max_pos = (await cursor.fetchone())[0]
            start = max_pos + 1
        else:
            cursor = await db.execute(
                "INSERT INTO playlists (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            playlist_id = cursor.lastrowid
            start = 0
        for i, t in enumerate(tracks):
            await db.execute(
                "INSERT INTO playlist_tracks (playlist_id, title, uri, author, duration, position) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (playlist_id, t["title"], t["uri"], t["author"], t.get("duration", 0), start + i),
            )
        await db.commit()
        return playlist_id


async def delete_playlist(user_id: int, name: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _connect(db)
        cursor = await db.execute(
            "DELETE FROM playlists WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_queue_state(guild_id: int, tracks: list[dict], position: int, loop_mode: str | None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO queue_state (guild_id, track_data, position, loop_mode) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET track_data = ?, position = ?, loop_mode = ?, saved_at = datetime('now')",
            (guild_id, json.dumps(tracks), position, loop_mode,
             json.dumps(tracks), position, loop_mode),
        )
        await db.commit()


async def load_queue_state(guild_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT track_data, position, loop_mode FROM queue_state WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "tracks": json.loads(row[0]),
            "position": row[1],
            "loop_mode": row[2],
        }


async def delete_queue_state(guild_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM queue_state WHERE guild_id = ?", (guild_id,))
        await db.commit()


async def spotify_cache_get(key: str) -> str | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT data FROM spotify_cache WHERE cache_key = ? AND expires_at > ?",
            (key, time.time()),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def spotify_cache_set(key: str, data: str, ttl: int = 3600):
    expires = time.time() + ttl
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO spotify_cache (cache_key, data, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET data = ?, expires_at = ?",
            (key, data, expires, data, expires),
        )
        await db.commit()


async def spotify_cache_cleanup():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM spotify_cache WHERE expires_at <= ?", (time.time(),))
        await db.commit()
