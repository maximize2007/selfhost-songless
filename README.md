# MADE WITH AI

# Songless SELF-HOSTED - MADE WITH AI

A song-guessing game built with Flask. Import a playlist from **YouTube, Spotify or Apple Music**, then guess each song from snippets that get longer every time you miss:

| Stage | 0.1 s | 0.5 s | 2 s | 8 s | 20 s |
|-------|-------|-------|-----|-----|------|
| Points | 100 | 80 | 60 | 40 | 20 |

A game has 5 songs. Save your score with your name to the highscore list, per playlist or for the "Play all" mix.

---

## Requirements

| Tool | Version | Why |
|------|---------|-----|
| [Python](https://www.python.org/downloads/) | 3.11 or newer | Runs the app |
| [FFmpeg](https://ffmpeg.org/) | any recent | Converts downloads to MP3 and cuts the snippets |
| [Deno](https://deno.com/) | 2.x | JavaScript runtime that yt-dlp needs for YouTube (see below) |

### Why Deno?

YouTube protects its download URLs with a JavaScript challenge. yt-dlp solves it by running YouTube's own code in a JavaScript runtime. **Without one, YouTube throttles downloads to a few KB/s** and you'll see this warning:

```
WARNING: [youtube] No supported JavaScript runtime could be found.
```

yt-dlp uses Deno by default. Node.js also works as a fallback: the app picks it automatically if Deno isn't installed.

---

## Installation

### Windows (PowerShell)

```powershell
# 1. Install FFmpeg and Deno
winget install --id Gyan.FFmpeg
winget install --id DenoLand.Deno
#    No winget? Install Deno with:  irm https://deno.land/install.ps1 | iex

# 2. Close and reopen the terminal (and VS Code / PyCharm) so the new PATH is loaded.
#    Then check:
ffmpeg -version
deno --version

# 3. Create a virtual environment and install the Python packages
cd songless
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 4. Start the app
python app.py
```

If `Activate.ps1` is blocked, allow local scripts once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### macOS

```bash
brew install ffmpeg deno

cd songless
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Linux

```bash
# Arch
sudo pacman -S ffmpeg deno
# Debian / Ubuntu
sudo apt install ffmpeg python3-venv && curl -fsSL https://deno.land/install.sh | sh

cd songless
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** in your browser.

### Check that everything works

When the app starts, it prints which JavaScript runtime it found:

```
[songless] JS-Runtime: deno (C:\Users\you\.deno\bin\deno.exe)
```

If it says `WARNING: no JS runtime found`, see [Troubleshooting](#troubleshooting).

---

## How to play

1. Paste a playlist link on the home page and click **Import playlist**.
   - YouTube: `https://www.youtube.com/playlist?list=…`
   - Spotify: `https://open.spotify.com/playlist/…`
   - Apple Music: `https://music.apple.com/…/playlist/…` (albums work too)
2. The songs download in the background, with a progress bar. You can start playing as soon as the first songs are ready.
3. Click **Play**, press the play button and type your guess. Pick a title from the suggestion list.
   A wrong guess or **Skip** unlocks the next, longer snippet.
4. After 5 songs, enter your name and save your highscore.

With two or more playlists, **Play all** mixes songs from all of them and has its own highscore list.

The ⟳ button downloads songs that were added to the playlist since the last import.

---

## How it works

- **YouTube** playlists are downloaded directly with yt-dlp.
- **Spotify and Apple Music** songs are DRM-protected and can't be downloaded. The app reads the track list (artist, title, duration) and searches each song on YouTube. From the top 5 results it picks the one whose length is closest to the original and skips live, cover, remix and slowed versions.
  - Spotify: read via [`spotifyscraper`](https://pypi.org/project/spotifyscraper/), no API key needed.
  - Apple Music: read from the public web page, no Apple developer account needed.
  - The playlist must be **public**. Very large Spotify playlists may only return the first 100 songs.
- Only the **first 30 seconds** of each song are downloaded, not the whole track.
- **Silence at the start** of a song (quieter than -60 dB) is detected once with FFmpeg, and snippets start at the first sound. Quiet intros that you can still hear are kept.
- The server only sends the snippet that's currently unlocked and calculates the score itself, so you can't cheat through the browser's developer tools.
- Playlists, songs and highscores are stored in a SQLite database (`songless.db`). Audio files are in `downloads/<playlist id>/`.

---

## Configuration

**`app.py`**

| Setting | Default | Meaning |
|---------|---------|---------|
| `STAGES` | `[0.1, 0.5, 2, 8, 20]` | Snippet lengths in seconds |
| `POINTS` | `[100, 80, 60, 40, 20]` | Points for guessing at each stage |
| `ROUNDS` | `5` | Songs per game |

**`downloader.py`**

| Setting | Default | Meaning |
|---------|---------|---------|
| `CLIP_SECONDS` | `30` | Seconds downloaded from the start of each song. Must be at least the last stage. |
| `SILENCE_DB` | `-60` | Anything quieter than this at the start counts as silence |
| `MAX_SONGS` | `200` | Maximum songs per playlist |

**Environment variables**

| Variable | Meaning |
|----------|---------|
| `DENO_PATH` | Full path to `deno.exe` if the app can't find Deno |
| `SECRET_KEY` | Flask session key. Set it to keep running games across restarts. |

---

## Troubleshooting

**Downloads are very slow (around 16–50 KB/s), or you see "No supported JavaScript runtime"**
yt-dlp can't find Deno.
1. Run `deno --version` in the same terminal where you start the app.
2. If it's not found, restart the terminal or your IDE completely. They only pick up the new PATH after a restart.
3. Make sure the yt-dlp extras are installed: `pip install -U "yt-dlp[default]"` (check with `pip show yt-dlp-ejs`).
4. If it still isn't found, set the path manually:
   ```powershell
   where.exe deno                       # Windows: find the path
   $env:DENO_PATH = "C:\path\to\deno.exe"
   python app.py
   ```

**YouTube downloads fail suddenly**
YouTube changes often. Update yt-dlp: `pip install -U "yt-dlp[default]"`

**`ffmpeg` not found**
Install FFmpeg (see above) and restart the terminal. `ffmpeg -version` must work.

**Spotify or Apple Music import finds no songs**
Make sure the playlist is public. Copy the link via **Share → Copy link** in the app.

**Some songs are "skipped"**
No good YouTube match was found, or the video is unavailable. The terminal shows the reason for each song.

**The page still looks old after an update**
Reload with **Ctrl + F5**.

**The download stopped when I closed the app**
Start the app again and press ⟳ on the playlist. Songs already downloaded are kept.

---

## Project structure

```
songless/
├── app.py            Routes, game logic, snippet output (FFmpeg)
├── downloader.py     Background downloads (yt-dlp, Spotify, Apple Music), silence detection
├── database.py       SQLite schema and migrations
├── requirements.txt
├── templates/        base, index, play, highscores
└── static/
    ├── game.js       Game UI and audio playback (Web Audio API)
    └── style.css
```

Downloading music may be restricted by the terms of service of the platforms. Use this project for private purposes only.
