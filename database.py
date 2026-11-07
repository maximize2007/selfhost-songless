import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "songless.db"

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
    source_id   TEXT,                     -- YouTube- oder Spotify-Track-ID
    youtube_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    duration    REAL,
    start_offset REAL,                    -- Sekunden Stille am Anfang (wird übersprungen)
    UNIQUE (playlist_id, youtube_id)
);
CREATE TABLE IF NOT EXISTS highscores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    score       INTEGER NOT NULL,
    rounds      INTEGER NOT NULL,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    is_mix      INTEGER DEFAULT 0,        -- 1 = aus allen Playlists gespielt
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as c:
        c.executescript(SCHEMA)
        # Migration für ältere Datenbanken
        if "source_id" not in {r["name"] for r in c.execute("PRAGMA table_info(songs)")}:
            c.execute("ALTER TABLE songs ADD COLUMN source_id TEXT")
        if "start_offset" not in {r["name"] for r in c.execute("PRAGMA table_info(songs)")}:
            c.execute("ALTER TABLE songs ADD COLUMN start_offset REAL")
        if "is_mix" not in {r["name"] for r in c.execute("PRAGMA table_info(highscores)")}:
            c.execute("ALTER TABLE highscores ADD COLUMN is_mix INTEGER DEFAULT 0")
        # Downloads, die beim letzten Beenden noch liefen, sind abgebrochen
        c.execute("UPDATE playlists SET status='error', message='Download unterbrochen – bitte Sync drücken' "
                  "WHERE status IN ('pending', 'downloading')")
