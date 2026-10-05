FROM python:3.12-slim

# ffmpeg: gabung video+audio & konversi MP3
# deno: dibutuhkan yt-dlp untuk membuka YouTube
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

CMD ["python", "-u", "bot.py"]
