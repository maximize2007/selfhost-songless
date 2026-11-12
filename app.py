import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import time
import unicodedata
import wave
from contextlib import contextmanager
from datetime import date, timedelta
from functools import lru_cache, wraps

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

from database import DATA_DIR, get_db, init_db
from downloader import (CLIP_SECONDS, DOWNLOAD_DIR, cancel_download, canonical_url, detect_start,
                        is_supported_url, start_download)

# ---------------- Settings ----------------
STAGES = [0.1, 0.5, 2, 8, 20]      # snippet lengths in seconds
POINTS = [100, 80, 60, 40, 20]     # points for guessing at that stage
ROUNDS = 5                         # songs per game (classic and daily)
EXACT_SECONDS = 3                  # snippets up to this length are sample-exact WAV, longer ones MP3 (10x smaller)
RATE = 44100
MODES = {                          # key: (label, description)
    "classic": ("Classic", f"{ROUNDS} random songs"),
    "daily": ("Daily", f"The same {ROUNDS} songs for everyone, new every day"),
    "survival": ("Survival", "Keep going until you miss a song"),
}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")   # if set, importing/syncing/deleting playlists needs a login

assert len(STAGES) == len(POINTS), "every stage needs its points"
assert STAGES[-1] <= CLIP_SECONDS, "the longest stage must not exceed CLIP_SECONDS"


def _secret_key() -> str:
    """SECRET_KEY from the environment, otherwise a key generated once and kept, so restarts don't end games."""
    if key := os.environ.get("SECRET_KEY"):
        return key
    path = DATA_DIR / ".secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


app = Flask(__name__)
app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
init_db()

if not shutil.which("ffmpeg"):
    print("[songless] WARNING: ffmpeg not found. Downloads and snippets will not work (see README).")


def clean_stored_urls():
    """Older versions stored links with their tracking parameters. Clean them, so re-pasting finds the playlist."""
    with get_db() as c:
        for row in c.execute("SELECT id, url FROM playlists").fetchall():
            if (url := canonical_url(row["url"])) != row["url"]:
                c.execute("UPDATE OR IGNORE playlists SET url=? WHERE id=?", (url, row["id"]))


clean_stored_urls()


def is_admin() -> bool:
    return not ADMIN_PASSWORD or session.get("admin", False)


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        return view(*args, **kwargs) if is_admin() else redirect(url_for("login"))
    return wrapper


@app.context_processor
def template_helpers():
    """{{ static('style.css') }} -> /static/style.css?v=<mtime>, so the browser never serves stale files."""
    def static(filename):
        path = os.path.join(app.static_folder, filename)
        return url_for("static", filename=filename, v=int(os.path.getmtime(path)))
    return {"static": static, "is_admin": is_admin(), "admin_enabled": bool(ADMIN_PASSWORD), "modes": MODES}


@app.before_request
def block_cross_site_posts():
    """Another website must not be able to delete playlists or post guesses through the visitor's browser."""
    if request.method == "POST" and request.headers.get("Sec-Fetch-Site") == "cross-site":
        abort(403)


# ---------------- Helpers ----------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


def artist(title: str) -> str:
    """'Artist - Song' -> normalized artist ('' if the title has no artist part)."""
    head, sep, _ = (title or "").partition(" - ")
    return norm(head) if sep else ""


def get_playlist(pid):
    with get_db() as c:
        p = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    return p or abort(404)


# ---------------- Game state ----------------
# The state lives in the database, the cookie only holds the game id. A cookie-only state could be rolled back
# by re-sending an old cookie (listen to all snippets, restore, guess at 0.1 s).
@contextmanager
def game(write=True):
    """Yields (connection, game). With write=True the game is locked, changed and saved in one transaction."""
    gid = session.get("gid")
    with get_db() as c:
        if write:
            c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT data FROM games WHERE id=?", (gid,)).fetchone() if gid else None
        if not row:
            abort(400, "No active game")
        g = json.loads(row["data"])
        yield c, g
        if write:
            c.execute("UPDATE games SET data=?, updated_at=datetime('now') WHERE id=?", (json.dumps(g), gid))


def current_song(c, g):
    song = c.execute("SELECT * FROM songs WHERE id=?", (g["songs"][g["round"]],)).fetchone()
    return song or abort(410, "This song was deleted")


def is_finished(g) -> bool:
    return g["done"] and (g["over"] or g["round"] == len(g["songs"]) - 1)


def game_state(c, g, result=None):
    st = {
        "mode": g["mode"], "day": g["day"], "round": g["round"] + 1, "rounds": len(g["songs"]),
        "stage": g["stage"], "stages": STAGES, "points": POINTS, "score": g["score"], "marks": g["marks"],
        "done": g["done"], "finished": is_finished(g), "saved": g["saved"], "history": g["history"],
        "result": result,
    }
    if g["done"]:
        song = current_song(c, g)
        st.update(answer=song["title"], youtube_id=song["youtube_id"], plays=song["plays"], hits=song["hits"])
    return st


MARKS = {"correct": "o", "artist": "a", "wrong": "x", "skipped": "s"}


def advance(c, g, verdict: str) -> str:
    """Applies one attempt (correct | artist | wrong | skipped) and returns the result shown to the player."""
    song = current_song(c, g)
    g["marks"].append(MARKS[verdict])
    if verdict != "correct" and g["stage"] < len(STAGES) - 1:
        g["stage"] += 1
        return verdict
    pts = POINTS[g["stage"]] if verdict == "correct" else 0
    g["score"] += pts
    g["done"] = True
    g["over"] = g["mode"] == "survival" and not pts
    g["history"].append({"title": song["title"], "time": STAGES[g["stage"]] if pts else None,
                         "points": pts, "marks": g["marks"]})
    c.execute("UPDATE songs SET plays = plays + 1, hits = hits + ? WHERE id=?", (int(pts > 0), song["id"]))
    return "correct" if pts else "failed"


# ---------------- Admin login (only with ADMIN_PASSWORD) ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if is_admin():
        return redirect(url_for("index"))
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = session.permanent = True
            return redirect(url_for("index"))
        time.sleep(1)                               # slows down guessing
    return render_template("login.html", error=request.method == "POST")


@app.post("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


# ---------------- Playlists ----------------
@app.get("/")
def index():
    with get_db() as c:
        playlists = c.execute(
            "SELECT p.*, COUNT(s.id) AS songs FROM playlists p "
            "LEFT JOIN songs s ON s.playlist_id = p.id GROUP BY p.id ORDER BY p.id DESC"
        ).fetchall()
        mix_count = c.execute("SELECT COUNT(DISTINCT title) FROM songs").fetchone()[0]
    with_songs = sum(1 for p in playlists if p["songs"])
    return render_template("index.html", playlists=playlists, mix_count=mix_count if with_songs >= 2 else 0)


@app.post("/playlist")
@admin_required
def add_playlist():
    url = canonical_url(request.form.get("url", ""))
    if not is_supported_url(url):
        return redirect(url_for("index", error="That is not a YouTube, Spotify or Apple Music link"))
    with get_db() as c:
        row = c.execute("SELECT id FROM playlists WHERE url=?", (url,)).fetchone()
        pid = row["id"] if row else c.execute("INSERT INTO playlists (url) VALUES (?)", (url,)).lastrowid
    start_download(pid, url)                        # does nothing if this playlist is already downloading
    return redirect(url_for("index"))


@app.post("/playlist/<int:pid>/sync")
@admin_required
def sync_playlist(pid):
    start_download(pid, get_playlist(pid)["url"])
    return redirect(url_for("index"))


@app.post("/playlist/<int:pid>/delete")
@admin_required
def delete_playlist(pid):
    cancel_download(pid)
    with get_db() as c:
        c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    shutil.rmtree(DOWNLOAD_DIR / str(pid), ignore_errors=True)
    return redirect(url_for("index"))


@app.get("/api/playlist/<int:pid>")
def playlist_status(pid):
    p = get_playlist(pid)
    return jsonify({k: p[k] for k in ("status", "progress", "total", "message")})


# ---------------- Game ----------------
def start_game(pid=None):
    """Creates a game and redirects to /game (so reloading the page doesn't restart it). pid=None -> all playlists."""
    mode = request.args.get("mode")
    mode = mode if mode in MODES else "classic"
    key, today = "all" if pid is None else str(pid), date.today().isoformat()
    session.permanent = True
    session.pop("game", None)                       # cookie state of older versions

    daily = {k: v for k, v in session.get("daily", {}).items() if v[0] == today}
    if mode == "daily" and key in daily:            # one attempt per day: reopen it instead of dealing again
        with get_db() as c:
            if c.execute("SELECT 1 FROM games WHERE id=?", (daily[key][1],)).fetchone():
                session["gid"] = daily[key][1]
                return redirect(url_for("game_page"))

    with get_db() as c:
        songs = c.execute("SELECT id, title FROM songs" + ("" if pid is None else " WHERE playlist_id=?")
                          + " ORDER BY id", () if pid is None else (pid,)).fetchall()
    if not songs:
        return redirect(url_for("index"))
    unique = sorted({norm(s["title"]): s["id"] for s in songs}.values())   # same song in several playlists: once
    if mode == "daily":
        picks = random.Random(f"{today}:{key}").sample(unique, min(ROUNDS, len(unique)))
    else:
        picks = random.sample(unique, len(unique) if mode == "survival" else min(ROUNDS, len(unique)))

    g = {"pid": pid, "mode": mode, "day": today if mode == "daily" else None, "songs": picks, "round": 0,
         "stage": 0, "score": 0, "marks": [], "done": False, "over": False, "saved": False, "history": []}
    gid = secrets.token_urlsafe(16)
    with get_db() as c:
        c.execute("DELETE FROM games WHERE updated_at < datetime('now', '-2 days')")
        c.execute("INSERT INTO games (id, data) VALUES (?, ?)", (gid, json.dumps(g)))
    session["gid"] = gid
    if mode == "daily":
        daily[key] = [today, gid]
    session["daily"] = daily
    return redirect(url_for("game_page"))


@app.get("/play/all")
def play_all():
    return start_game(None)


@app.get("/play/<int:pid>")
def play(pid):
    get_playlist(pid)
    return start_game(pid)


@app.get("/game")
def game_page():
    gid = session.get("gid")
    with get_db() as c:
        row = c.execute("SELECT data FROM games WHERE id=?", (gid,)).fetchone() if gid else None
        if not row:
            return redirect(url_for("index"))
        g = json.loads(row["data"])
        pid = g["pid"]
        playlist = c.execute("SELECT title FROM playlists WHERE id=?", (pid,)).fetchone() if pid else None
        if pid and not playlist:                    # playlist was deleted during the game
            return redirect(url_for("index"))
        titles = [r[0] for r in c.execute("SELECT DISTINCT title FROM songs"
                                          + (" WHERE playlist_id=?" if pid else ""), (pid,) if pid else ())]
    args = {"mode": g["mode"]}
    return render_template(
        "play.html",
        title=playlist["title"] if pid else "All playlists",
        mode=g["mode"],
        titles=sorted(titles, key=str.casefold),
        replay_url=url_for("play", pid=pid, **args) if pid else url_for("play_all", **args),
        hs_url=url_for("highscores", playlist=pid or "all", **args),
    )


@app.get("/api/state")
def state():
    with game(write=False) as (c, g):
        return jsonify(game_state(c, g))


# ---------------- Snippets ----------------
def _ffmpeg(*args) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", *args, "pipe:1"], capture_output=True, check=True).stdout


@lru_cache(maxsize=32)
def _pcm(path: str, offset: float, seconds: float, mtime: int) -> bytes:
    """Start of a song as raw PCM. Decoded once per song; every short snippet is then just a slice of it."""
    return _ffmpeg("-i", path, "-ss", str(offset), "-t", str(seconds), "-f", "s16le", "-ac", "2", "-ar", str(RATE))


def make_clip(path, offset: float, seconds: float):
    """Returns (data, mimetype) of the snippet, starting at the first sound of the song."""
    if seconds > EXACT_SECONDS and path.suffix.lower() == ".mp3":
        # Long snippet: cut the MP3 without re-encoding (a 20 s WAV would be 3.5 MB). MP3 can only be cut at
        # frame borders, so the start may be off by a few hundredths of a second. That doesn't matter at this length.
        seek = ("-ss", str(offset)) if offset > 0 else ()
        return _ffmpeg("-i", str(path), *seek, "-t", str(seconds), "-c", "copy",
                       "-write_xing", "0", "-id3v2_version", "0", "-f", "mp3"), "audio/mpeg"
    span = max(seconds, EXACT_SECONDS)
    pcm = _pcm(str(path), offset, span, path.stat().st_mtime_ns)[: round(seconds * RATE) * 4]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue(), "audio/wav"


@app.get("/api/clip")
def clip():
    """Serves only the currently unlocked snippet (no peeking via the network tab)."""
    with game(write=False) as (c, g):
        song = current_song(c, g)
        length = STAGES[-1] if g["done"] else STAGES[g["stage"]]
    path = DOWNLOAD_DIR / song["filename"]
    if not path.exists():
        abort(404)
    offset = song["start_offset"]
    if offset is None:                      # older downloads: measure leading silence once
        offset = detect_start(path)
        with get_db() as c:
            c.execute("UPDATE songs SET start_offset=? WHERE id=?", (offset, song["id"]))
    try:
        data, mimetype = make_clip(path, offset, length)
    except (OSError, subprocess.CalledProcessError) as ex:
        abort(500, f"ffmpeg failed: {ex}")
    return Response(data, mimetype=mimetype, headers={"Cache-Control": "no-store"})


# ---------------- Guessing ----------------
@app.post("/api/guess")
def guess():
    text = str((request.get_json(silent=True) or {}).get("guess", ""))
    with game() as (c, g):
        if g["done"]:
            abort(400)
        title = current_song(c, g)["title"]
        if norm(text) and norm(text) == norm(title):
            verdict = "correct"
        elif artist(text) and artist(text) == artist(title):
            verdict = "artist"                      # wrong song, right artist: counts as a miss but tells the player
        else:
            verdict = "wrong"
        return jsonify(game_state(c, g, advance(c, g, verdict)))


@app.post("/api/skip")
def skip():
    with game() as (c, g):
        if g["done"]:
            abort(400)
        return jsonify(game_state(c, g, advance(c, g, "skipped")))


@app.post("/api/next")
def next_round():
    with game() as (c, g):
        if not g["done"] or is_finished(g):
            abort(400)
        g.update(round=g["round"] + 1, stage=0, marks=[], done=False)
        return jsonify(game_state(c, g))


# ---------------- Highscores ----------------
@app.post("/api/highscore")
def save_highscore():
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()[:20]
    with game() as (c, g):
        if not name or g["saved"] or not is_finished(g):
            abort(400)
        hits = sum(1 for h in g["history"] if h["points"])
        hid = c.execute(
            "INSERT INTO highscores (name, score, rounds, playlist_id, is_mix, mode, day) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, g["score"], hits if g["mode"] == "survival" else len(g["history"]),
             g["pid"], int(g["pid"] is None), g["mode"], g["day"]),
        ).lastrowid
        g["saved"] = True
    return jsonify({"id": hid})


@app.get("/highscores")
def highscores():
    sel, mode = request.args.get("playlist", ""), request.args.get("mode")
    mode = mode if mode in MODES else "classic"
    where, args = ["h.mode = ?"], [mode]
    if mode == "daily":                             # every day has its own songs, so only today's runs compare
        where.append("h.day = ?")
        args.append(date.today().isoformat())
    if sel == "all":
        where.append("h.is_mix = 1")
    elif sel.isdigit():
        where.append("h.playlist_id = ? AND h.is_mix = 0")
        args.append(int(sel))
    with get_db() as c:
        scores = c.execute(
            "SELECT h.*, p.title AS playlist FROM highscores h LEFT JOIN playlists p ON p.id = h.playlist_id "
            f"WHERE {' AND '.join(where)} ORDER BY h.score DESC, h.created_at ASC LIMIT 50", args).fetchall()
        playlists = c.execute("SELECT id, title, url FROM playlists ORDER BY title").fetchall()
    return render_template("highscores.html", scores=scores, playlists=playlists, sel=sel, mode=mode,
                           max_points=POINTS[0], hl=request.args.get("hl", type=int))


if __name__ == "__main__":
    # DEBUG=1 enables the reloader and the interactive debugger. Never use it on a server others can reach.
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("DEBUG") == "1", threaded=True)
