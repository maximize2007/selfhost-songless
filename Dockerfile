FROM python:3.12-slim

# FFmpeg converts and cuts the audio, Deno runs YouTube's JavaScript challenge for yt-dlp
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .

# database, downloads and the session key are kept in the volume
ENV SONGLESS_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 5000

# One process with many threads: the downloads run in background threads of this process.
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:5000", "app:app"]
