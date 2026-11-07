import io
import os
import random
import re
import secrets
import shutil
import subprocess
import unicodedata
import wave

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

from database import get_db, init_db
from downloader import CLIP_SECONDS, DOWNLOAD_DIR, detect_start, is_supported_url, start_download

# ---------------- Konfiguration ----------------
STAGES = [0.1, 0.5, 2, 8, 20]      # Snippet-Längen in Sekunden
POINTS = [100, 80, 60, 40, 20]     # Punkte, wenn in dieser Stufe erraten
ROUNDS = 5                         # Songs pro Spiel

assert STAGES[-1] <= CLIP_SECONDS, "längste Stufe darf nicht länger als CLIP_SECONDS sein"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))
init_db()


@app.context_processor
def static_helper():
    """{{ static('style.css') }} -> /static/style.css?v=<Änderungszeit>, damit der Browser nie alte Dateien nimmt."""
    def static(filename):
        path = os.path.join(app.static_folder, filename)
        return url_for("static", filename=filename, v=int(os.path.getmtime(path)))
    return {"static": static}


# ---------------- Helfer ----------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


def get_playlist(pid):
    with get_db() as c:
        p = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    return p or abort(404)


def get_game():
    g = session.get("game")
    if not g:
        abort(400, "Kein aktives Spiel")
    return g


def current_song(g):
    with get_db() as c:
        return c.execute("SELECT * FROM songs WHERE id=?", (g["songs"][g["round"]],)).fetchone()


def game_state(g, result=None):
    st = {
        "round": g["round"] + 1, "rounds": len(g["songs"]), "stage": g["stage"],
        "stages": STAGES, "points": POINTS, "score": g["score"], "done": g["done"],
        "finished": g["done"] and g["round"] == len(g["songs"]) - 1,
        "saved": g["saved"], "history": g["history"], "result": result,
    }
    if g["done"]:
        song = current_song(g)
        st.update(answer=song["title"], youtube_id=song["youtube_id"])
    return st


def advance(g, correct: bool) -> str:
    song = current_song(g)
    if correct:
        pts = POINTS[g["stage"]]
        g["score"] += pts
        g["done"] = True
        g["history"].append({"title": song["title"], "time": STAGES[g["stage"]], "points": pts})
        return "correct"
    if g["stage"] < len(STAGES) - 1:
        g["stage"] += 1
        return "wrong"
    g["done"] = True
    g["history"].append({"title": song["title"], "time": None, "points": 0})
    return "failed"


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
def add_playlist():
    url = request.form.get("url", "").strip()
    if not is_supported_url(url):
        return redirect(url_for("index", error="Das ist kein Link zu YouTube, Spotify oder Apple Music"))
    with get_db() as c:
        row = c.execute("SELECT id, status FROM playlists WHERE url=?", (url,)).fetchone()
        pid = row["id"] if row else c.execute("INSERT INTO playlists (url) VALUES (?)", (url,)).lastrowid
    if not row or row["status"] != "downloading":
        start_download(pid, url)
    return redirect(url_for("index"))


@app.post("/playlist/<int:pid>/sync")
def sync_playlist(pid):
    p = get_playlist(pid)
    if p["status"] != "downloading":
        start_download(pid, p["url"])
    return redirect(url_for("index"))


@app.post("/playlist/<int:pid>/delete")
def delete_playlist(pid):
    with get_db() as c:
        c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    shutil.rmtree(DOWNLOAD_DIR / str(pid), ignore_errors=True)
    return redirect(url_for("index"))


@app.get("/api/playlist/<int:pid>")
def playlist_status(pid):
    p = get_playlist(pid)
    return jsonify({k: p[k] for k in ("status", "progress", "total", "message")})


# ---------------- Spiel ----------------
def start_game(pid=None):
    """pid=None -> Mix aus allen Playlists."""
    with get_db() as c:
        if pid is None:
            songs = c.execute("SELECT * FROM songs").fetchall()
        else:
            songs = c.execute("SELECT * FROM songs WHERE playlist_id=?", (pid,)).fetchall()
    if not songs:
        return redirect(url_for("index"))
    unique = list({norm(s["title"]): s for s in songs}.values())   # gleiche Songs aus mehreren Playlists nur 1x
    picks = random.sample(unique, min(ROUNDS, len(unique)))
    session["game"] = {
        "pid": pid, "mix": pid is None,
        "songs": [s["id"] for s in picks],
        "round": 0, "stage": 0, "score": 0, "done": False, "saved": False, "history": [],
    }
    key = "all" if pid is None else pid
    return render_template(
        "play.html",
        title="Alle Playlists" if pid is None else get_playlist(pid)["title"],
        titles=sorted({s["title"] for s in songs}, key=str.casefold),
        replay_url=url_for("play_all") if pid is None else url_for("play", pid=pid),
        hs_url=url_for("highscores", playlist=key),
    )


@app.get("/play/all")
def play_all():
    return start_game(None)


@app.get("/play/<int:pid>")
def play(pid):
    get_playlist(pid)
    return start_game(pid)


@app.get("/api/state")
def state():
    return jsonify(game_state(get_game()))


@app.get("/api/clip")
def clip():
    """Liefert nur den aktuell freigeschalteten Ausschnitt (kein Spicken im Netzwerk-Tab)."""
    g = get_game()
    song = current_song(g)
    path = DOWNLOAD_DIR / song["filename"]
    if not path.exists():
        abort(404)
    length = STAGES[-1] if g["done"] else STAGES[g["stage"]]
    offset = song["start_offset"]
    if offset is None:                      # ältere Downloads: Stille einmalig nachträglich messen
        offset = detect_start(path)
        with get_db() as c:
            c.execute("UPDATE songs SET start_offset=? WHERE id=?", (offset, song["id"]))
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(offset), "-t", str(length), "-i", str(path),  # ab dem ersten Ton
         "-f", "s16le", "-ac", "2", "-ar", "44100", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(pcm)
    return Response(buf.getvalue(), mimetype="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/api/guess")
def guess():
    g = get_game()
    if g["done"]:
        abort(400)
    text = (request.get_json(silent=True) or {}).get("guess", "")
    g_norm = norm(text)
    result = advance(g, bool(g_norm) and g_norm == norm(current_song(g)["title"]))
    session["game"] = g
    return jsonify(game_state(g, result))


@app.post("/api/skip")
def skip():
    g = get_game()
    if g["done"]:
        abort(400)
    result = advance(g, False)
    session["game"] = g
    return jsonify(game_state(g, "skipped" if result == "wrong" else result))


@app.post("/api/next")
def next_round():
    g = get_game()
    if not g["done"] or g["round"] >= len(g["songs"]) - 1:
        abort(400)
    g.update(round=g["round"] + 1, stage=0, done=False)
    session["game"] = g
    return jsonify(game_state(g))


# ---------------- Highscores ----------------
@app.post("/api/highscore")
def save_highscore():
    g = get_game()
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:20]
    if not name or g["saved"] or not game_state(g)["finished"]:
        abort(400)
    with get_db() as c:
        hid = c.execute(
            "INSERT INTO highscores (name, score, rounds, playlist_id, is_mix) VALUES (?, ?, ?, ?, ?)",
            (name, g["score"], len(g["songs"]), g["pid"], int(g.get("mix", False))),
        ).lastrowid
    g["saved"] = True
    session["game"] = g
    return jsonify({"id": hid})


@app.get("/highscores")
def highscores():
    sel = request.args.get("playlist", "")
    sql = "SELECT h.*, p.title AS playlist FROM highscores h LEFT JOIN playlists p ON p.id = h.playlist_id"
    args = ()
    if sel == "all":
        sql += " WHERE h.is_mix = 1"
    elif sel.isdigit():
        sql += " WHERE h.playlist_id = ? AND h.is_mix = 0"
        args = (int(sel),)
    sql += " ORDER BY h.score DESC, h.created_at ASC LIMIT 50"
    with get_db() as c:
        scores = c.execute(sql, args).fetchall()
        playlists = c.execute("SELECT id, title, url FROM playlists ORDER BY title").fetchall()
    return render_template("highscores.html", scores=scores, playlists=playlists, sel=sel,
                           hl=request.args.get("hl", type=int))


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
