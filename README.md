# Songless (Flask)

Errate Songs aus deinen Playlists von YouTube, Spotify oder Apple Music anhand kurzer Ausschnitte:
**0.1s → 0.5s → 2s → 8s → 20s** (100 / 80 / 60 / 40 / 20 Punkte).

## Setup

```bash
# ffmpeg wird von yt-dlp und für die Snippets gebraucht
sudo pacman -S ffmpeg deno        # Arch  (Debian/Ubuntu: apt install ffmpeg)

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py                     # → http://127.0.0.1:5000
```

> yt-dlp braucht für YouTube inzwischen eine JS-Runtime (Deno empfohlen).
> Wenn Downloads fehlschlagen: `pip install -U "yt-dlp[default]"` und Deno installieren.

## Ablauf

1. Playlist-URL einfügen → Songs werden im Hintergrund nach `downloads/<id>/` geladen (MP3).
2. **▶ Spielen** → 5 zufällige Songs. Falsch raten oder Überspringen schaltet die nächste Stufe frei.
3. Am Ende Namen eingeben → Highscore wird in `songless.db` (SQLite) gespeichert.

## Konfiguration (`app.py`)

| Variable       | Bedeutung                                      |
|----------------|------------------------------------------------|
| `STAGES`       | Snippet-Längen in Sekunden                     |
| `POINTS`       | Punkte pro Stufe                               |
| `ROUNDS`       | Songs pro Spiel                                |

In `downloader.py`: `MAX_SONGS` begrenzt die Songs pro Playlist, `CLIP_SECONDS` (30) wie viele Sekunden ab Songanfang geladen werden, `SILENCE_DB` (-60) ab welcher Lautstärke Stille am Anfang übersprungen wird.

Spotify und Apple Music: Die Titelliste wird ausgelesen, jeder Song wird auf YouTube gesucht. Die Playlist muss öffentlich sein.

## Struktur

```
app.py          Routen, Spiellogik, Snippet-Ausgabe (ffmpeg)
downloader.py   yt-dlp Hintergrund-Download
database.py     SQLite-Schema (playlists, songs, highscores)
templates/      index, play, highscores
static/         game.js (Web Audio), style.css
```

Der Server liefert immer nur den gerade freigeschalteten Ausschnitt aus, Spielstand und Punkte werden
serverseitig berechnet – Spicken über die DevTools bringt also nichts.
