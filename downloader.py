import html
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from database import get_db

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
MAX_SONGS = 200
CLIP_SECONDS = 30            # seconds downloaded from the start of each song (buffer for leading silence)
SILENCE_DB = -60            # anything quieter than this at the start counts as silence and is skipped

SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?playlist/|spotify:playlist:)([A-Za-z0-9]+)")
APPLE_RE = re.compile(r"^https?://(?:embed\.)?music\.apple\.com/[a-z]{2}/(?:playlist|album)/", re.I)
YOUTUBE_RE = re.compile(r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/", re.I)

_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|remaster(ed)?|mv|m/v)\b[^\)\]]*[\)\]]",
    re.I,
)
_BAD_MATCH = re.compile(r"\b(live|cover|karaoke|instrumental|remix|8d|slowed|sped up|reverb|nightcore)\b", re.I)


# ---------------- JS runtime for YouTube ----------------
def find_js_runtime() -> dict:
    """Finds Deno (even if PATH is outdated), otherwise Node.js. Override with the DENO_PATH env variable."""
    candidates = [
        os.environ.get("DENO_PATH"),
        shutil.which("deno"),
        os.path.expandvars(r"%USERPROFILE%\.deno\bin\deno.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\deno.exe"),
        os.path.expanduser("~/.deno/bin/deno"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            print(f"[songless] JS-Runtime: deno ({p})")
            return {"deno": {"path": p}}
    if node := shutil.which("node"):
        print(f"[songless] JS-Runtime: node ({node})")
        return {"node": {"path": node}}
    print("[songless] WARNING: no JS runtime found, YouTube downloads will be throttled. Install Deno (see README).")
    return {"deno": {}}


JS_RUNTIMES = find_js_runtime()


# ---------------- Leading silence ----------------
def detect_start(path) -> float:
    """Second at which the first sound starts after leading silence (0.0 if sound starts immediately)."""
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-t", str(CLIP_SECONDS), "-i", str(path),
         "-af", f"silencedetect=noise={SILENCE_DB}dB:d=0.05", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    start = re.search(r"silence_start: (-?[\d.]+)", res.stderr)
    end = re.search(r"silence_end: ([\d.]+)", res.stderr)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", res.stderr)
    total = min(int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3]), CLIP_SECONDS) if dur else CLIP_SECONDS
    if start and end and float(start.group(1)) <= 0.05:
        offset = float(end.group(1))
        if offset < total - 0.5:                 # completely silent file -> don't cut
            return round(offset, 3)
    return 0.0


# ---------------- Titles ----------------
def clean_title(info: dict) -> str:
    """'Artist - Song (Official Video) [HD]' -> 'Artist - Song'"""
    track, artist = info.get("track"), info.get("artist") or info.get("creator")
    if track and artist:
        return f"{artist.split(',')[0].strip()} - {track}"
    title = info.get("title") or info.get("id")
    t = _JUNK.sub("", title)
    t = re.sub(r"\s*\|.*$", "", t)
    return re.sub(r"\s{2,}", " ", t).strip() or title


def clean_track_name(name: str) -> str:
    """'Song - Remastered 2011' / 'Song - Radio Edit' -> 'Song'"""
    t = re.sub(r"\s+-\s+.*\b(remaster(ed)?|edit|version|mono|stereo|mix)\b.*$", "", name, flags=re.I)
    return _JUNK.sub("", t).strip() or name


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").casefold())


# ---------------- Sources ----------------
def is_spotify(url: str) -> bool:
    return bool(SPOTIFY_RE.search(url))


def is_apple(url: str) -> bool:
    return bool(APPLE_RE.match(url))


def is_supported_url(url: str) -> bool:
    return bool(YOUTUBE_RE.match(url)) or is_spotify(url) or is_apple(url)


def _search_job(key, artist, name, duration_s):
    name = clean_track_name(name)
    return {"key": key, "title": f"{artist} - {name}", "label": f"{artist} - {name}",
            "query": f"{artist} {name} audio", "duration": duration_s}


def _youtube_jobs(url):
    with yt_dlp.YoutubeDL({"extract_flat": "in_playlist", "quiet": True, "js_runtimes": JS_RUNTIMES}) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or [info]) if e and e.get("id")][:MAX_SONGS]
    jobs = [{"key": e["id"], "yt_id": e["id"], "label": e.get("title") or e["id"]} for e in entries]
    return info.get("title") or url, jobs


def _spotify_jobs(url):
    from spotify_scraper import SpotifyClient

    with SpotifyClient() as client:
        pl = client.get_playlist(SPOTIFY_RE.search(url).group(1), max_tracks=MAX_SONGS)
    jobs = []
    for item in pl.tracks:
        t = item.track
        if t and getattr(t, "artists", None):           # skip local files, podcasts, …
            jobs.append(_search_job(t.id, t.artists[0].name, t.name, (t.duration_ms or 0) / 1000))
    return pl.name, jobs


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _iso_seconds(d):
    m = re.match(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", d or "")
    return (int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3] or 0)) if m else 0


def _apple_jobs(url):
    """Reads the track list from the public Apple Music web page (no account or token needed)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")

    m = re.search(r'<meta (?:property|name)="og:title" content="([^"]*)"', page)
    title = html.unescape(m.group(1)) if m else url
    title = re.sub(r"\s+(?:on|auf|bei) Apple\s*Music$", "", title, flags=re.I)

    jobs, seen = [], set()

    def add(key, artist, name, dur):
        k = _norm(f"{artist}{name}")
        if artist and name and k not in seen:
            seen.add(k)
            jobs.append(_search_job(f"am:{key or k}", artist, name, dur))

    # 1) internal page data (most complete source)
    m = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', page, re.S)
    if m:
        try:
            for d in _walk(json.loads(m.group(1))):
                if isinstance(d.get("title"), str) and isinstance(d.get("artistName"), str) \
                        and isinstance(d.get("duration"), (int, float)):
                    dur = d["duration"] / 1000 if d["duration"] > 10_000 else d["duration"]
                    add(d.get("id"), d["artistName"], d["title"], dur)
        except json.JSONDecodeError:
            pass

    # 2) Fallback: schema.org JSON-LD
    if not jobs:
        for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            for d in _walk(data):
                if d.get("@type") == "MusicRecording" and d.get("name"):
                    by = d.get("byArtist") or {}
                    by = by[0] if isinstance(by, list) and by else by
                    artist = by.get("name") if isinstance(by, dict) else None
                    add(d.get("url"), artist, d["name"], _iso_seconds(d.get("duration")))

    if not jobs:
        raise ValueError("No songs found. Is the playlist public?")
    return title, jobs[:MAX_SONGS]


def _find_on_youtube(ydl, query, duration):
    """Best of the top 5 results: duration closest to the original, no live/cover versions."""
    res = ydl.extract_info(f"ytsearch5:{query}", download=False)
    cands = [e for e in (res.get("entries") or []) if e and e.get("id")]
    if not cands:
        return None

    def score(e):
        d = e.get("duration") or 0
        diff = abs(d - duration) if d and duration else 30
        bad = _BAD_MATCH.search(e.get("title") or "") and not _BAD_MATCH.search(query)
        return diff + (120 if bad else 0)

    return min(cands, key=score)["id"]


# ---------------- Download ----------------
def _set(pid: int, **cols):
    sql = ", ".join(f"{k}=?" for k in cols)
    with get_db() as c:
        c.execute(f"UPDATE playlists SET {sql} WHERE id=?", (*cols.values(), pid))


def start_download(pid: int, url: str):
    _set(pid, status="downloading", progress=0, message="Starting…")
    threading.Thread(target=_download, args=(pid, url), daemon=True).start()


def _download(pid: int, url: str):
    try:
        _set(pid, message="Reading playlist…")
        if is_spotify(url):
            title, jobs = _spotify_jobs(url)
        elif is_apple(url):
            title, jobs = _apple_jobs(url)
        else:
            title, jobs = _youtube_jobs(url)
        _set(pid, title=title, total=len(jobs))

        with get_db() as c:
            have = {r[0] for r in c.execute(
                "SELECT COALESCE(source_id, youtube_id) FROM songs WHERE playlist_id=?", (pid,))}

        out_dir = DOWNLOAD_DIR / str(pid)
        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            # download only the first CLIP_SECONDS instead of the whole song
            "download_ranges": download_range_func(None, [(0, CLIP_SECONDS)]),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "5"}],
            "noplaylist": True,
            "js_runtimes": JS_RUNTIMES,
            "quiet": True,
            "noprogress": True,
        }
        search_opts = {"extract_flat": True, "quiet": True, "js_runtimes": JS_RUNTIMES}
        errors = 0
        with yt_dlp.YoutubeDL(opts) as ydl, yt_dlp.YoutubeDL(search_opts) as search:
            for i, job in enumerate(jobs, 1):
                _set(pid, progress=i - 1, message=f"Downloading {i}/{len(jobs)}: {job['label']}")
                if job["key"] in have:
                    continue
                try:
                    yt_id = job.get("yt_id") or _find_on_youtube(search, job["query"], job["duration"])
                    if not yt_id:
                        raise LookupError("no YouTube match")
                    v = ydl.extract_info(f"https://www.youtube.com/watch?v={yt_id}", download=True)
                    path = Path((v.get("requested_downloads") or [{}])[0].get("filepath")
                                or out_dir / f"{v['id']}.mp3")
                    if not path.exists():
                        raise FileNotFoundError(path)
                except Exception as ex:
                    print(f"[songless] skipped: {job['label']} ({ex})")
                    errors += 1
                    continue
                with get_db() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO songs "
                        "(playlist_id, source_id, youtube_id, title, filename, duration, start_offset) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (pid, job["key"], v["id"], job.get("title") or clean_title(v),
                         path.relative_to(DOWNLOAD_DIR).as_posix(),
                         min(v.get("duration") or CLIP_SECONDS, CLIP_SECONDS), detect_start(path)),
                    )

        with get_db() as c:
            n = c.execute("SELECT COUNT(*) FROM songs WHERE playlist_id=?", (pid,)).fetchone()[0]
        msg = f"{n} songs ready" + (f", {errors} skipped" if errors else "")
        _set(pid, status="ready" if n else "error", progress=len(jobs), message=msg)
    except Exception as ex:
        _set(pid, status="error", message=f"Error: {str(ex)[:300]}")
