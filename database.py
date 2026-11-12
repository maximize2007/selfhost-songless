import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Database, downloads and the session key live here. Set SONGLESS_DATA to keep them outside the code folder (Docker).
DATA_DIR = Path(os.environ.get("SONGLESS_DATA") or Path(__file__).parent)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "songless.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT UNIQUE NOT NULL,
    title      TEXT,
    status     TEXT DEFAULT 'pending',      -- pending | downloading | ready | error
    progress   INTEGER DEFAULT 0,
    total      INTEGER DEFAULT 0,
    message    TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    source_id   TEXT,                     -- YouTube, Spotify or Apple Music track ID
    youtube_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    duration    REAL,
    start_offset REAL,                    -- seconds of leading silence (skipped)
    plays       INTEGER DEFAULT 0,        -- how often the song was played to the end of a round
    hits        INTEGER DEFAULT 0,        -- how often it was guessed
    UNIQUE (playlist_id, youtube_id)
);
CREATE TABLE IF NOT EXISTS highscores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    score       INTEGER NOT NULL,
    rounds      INTEGER NOT NULL,         -- songs played (survival: songs guessed)
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    is_mix      INTEGER DEFAULT 0,        -- 1 = played as mix of all playlists
    mode        TEXT DEFAULT 'classic',   -- classic | daily | survival
    day         TEXT,                     -- daily challenge: the date it belongs to
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS games (        -- running games (state stays on the server, the cookie only holds the id)
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_songs_youtube ON songs(youtube_id);
"""

# columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("songs", "source_id", "TEXT"),
    ("songs", "start_offset", "REAL"),
    ("songs", "plays", "INTEGER DEFAULT 0"),
    ("songs", "hits", "INTEGER DEFAULT 0"),
    ("highscores", "is_mix", "INTEGER DEFAULT 0"),
    ("highscores", "mode", "TEXT DEFAULT 'classic'"),
    ("highscores", "day", "TEXT"),
]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as c:
        c.execute("PRAGMA journal_mode = WAL")       # downloads can write while the game reads
        for table, column, definition in MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:           # no columns = new database, the schema below creates it
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        c.executescript(SCHEMA)
        # downloads still running at the last shutdown were interrupted
        c.execute("UPDATE playlists SET status='error', message='Download interrupted. Press sync to resume.' "
                  "WHERE status IN ('pending', 'downloading')")
        c.execute("DELETE FROM games WHERE updated_at < datetime('now', '-2 days')")
