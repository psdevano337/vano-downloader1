"""Bot Telegram: downloader YouTube + TikTok (Video MP4 / Audio MP3).

Environment variables (isi di Railway -> Variables):
  BOT_TOKEN   wajib     token dari @BotFather (JANGAN ditulis di kode / GitHub)
  YT_COOKIES  opsional  isi file cookies.txt (format Netscape) bila YouTube minta login
  YT_PROXY    opsional  mis. http://user:pass@host:port bila IP server diblokir YouTube
"""
import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # supaya token tidak bocor ke log
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Variabel BOT_TOKEN belum diisi (Railway -> Variables).")
COOKIES = os.environ.get("YT_COOKIES", "").strip()
PROXY = os.environ.get("YT_PROXY", "").strip()

MAX_BYTES = 49 * 1024 * 1024     # batas upload bot Telegram +-50 MB
MAX_VIDEO = 10 * 60              # detik
MAX_AUDIO = 40 * 60              # detik
KUALITAS = [720, 480, 360, 240]  # urutan percobaan; turun otomatis bila file > 50 MB
DOMAIN_OK = ("youtube.com", "youtu.be", "tiktok.com")
SLOT = asyncio.Semaphore(3)      # maksimal 3 unduhan berjalan bersamaan
URL_RE = re.compile(r"https?://[^\s<>]+")


class GagalUnduh(Exception):
    """Pesan error yang aman ditampilkan ke pengguna."""


# ----------------------------------------------------------------- helper


def cari_url(teks: str) -> str | None:
    """Ambil link YouTube/TikTok pertama dari teks (domain lain ditolak)."""
    for u in URL_RE.findall(teks or ""):
        u = u.rstrip(".,;:!?)]}\"'")
        host = (urlparse(u).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in DOMAIN_OK):
            return u
    return None


def _pesan_error(e: Exception) -> str:
    log.warning("yt-dlp error: %s", e)
    t = str(e).lower()
    if "not a bot" in t or "sign in" in t:
        return (
            "YouTube meminta login / memblokir IP server. "
            "Admin perlu mengisi YT_COOKIES atau YT_PROXY di Railway."
        )
    if "private" in t:
        return "Video ini privat."
    if "unavailable" in t or "removed" in t or "not available" in t:
        return "Video tidak tersedia (dihapus / dibatasi wilayah)."
    if "unsupported url" in t:
        return "Link tidak didukung."
    return "Gagal mengunduh. Pastikan link benar dan videonya publik."


def _opsi_dasar(folder: Path) -> dict:
    opsi = {
        "outtmpl": str(folder / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if PROXY:
        opsi["proxy"] = PROXY
    if COOKIES:
        f = folder / "cookies.txt"  # salinan per unduhan, dihapus bersama folder
        f.write_text(COOKIES + "\n")
        opsi["cookiefile"] = str(f)
    return opsi


def _file_hasil(folder: Path) -> Path | None:
    kandidat = [
        p
        for p in folder.iterdir()
        if p.is_file() and p.name != "cookies.txt" and p.suffix not in (".part", ".ytdl")
    ]
    return max(kandidat, key=lambda p: p.stat().st_size, default=None)


def _jalankan(url: str, opsi: dict) -> dict:
    try:
        with yt_dlp.YoutubeDL(opsi) as ydl:
            return ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise GagalUnduh(_pesan_error(e)) from e


def unduh(url: str, mode: str, folder: Path) -> tuple[Path, dict]:
    """Jalan di thread terpisah. mode: 'v' = video MP4, 'a' = audio MP3."""
    dasar = _opsi_dasar(folder)

    # 1) Cek info dulu (tanpa unduh): live, playlist, durasi
    try:
        with yt_dlp.YoutubeDL(dasar) as ydl:
            cek = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise GagalUnduh(_pesan_error(e)) from e

    if cek.get("_type") == "playlist" or cek.get("is_live"):
        raise GagalUnduh("Kirim link satu video saja (bukan playlist / live).")
    durasi = cek.get("duration") or 0
    batas = MAX_AUDIO if mode == "a" else MAX_VIDEO
    if durasi > batas:
        raise GagalUnduh(
            f"Durasi terlalu panjang (maks {batas // 60} menit) "
            "karena Telegram membatasi file dari bot sampai 50 MB."
        )

    # 2) Audio MP3
    if mode == "a":
        info = _jalankan(
            url,
            dasar
            | {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",
                    }
                ],
            },
        )
        hasil = _file_hasil(folder)
        if hasil is None:
            raise GagalUnduh("File hasil unduhan tidak ditemukan.")
        if hasil.stat().st_size > MAX_BYTES:
            raise GagalUnduh("File terlalu besar untuk dikirim lewat bot (maks 50 MB).")
        return hasil, info

    # 3) Video MP4: mulai dari kualitas tinggi, turun bila file kegedean.
    #    Video panjang langsung mulai dari kualitas lebih rendah (hemat waktu).
    mulai = 0 if durasi <= 180 else 1 if durasi <= 420 else 2
    for tinggi in KUALITAS[mulai:]:
        info = _jalankan(
            url,
            dasar
            | {
                "format": "bv*+ba/b",
                # 'res' = sisi terkecil, jadi aman untuk video vertikal (TikTok/Shorts)
                "format_sort": [f"res:{tinggi}", "vcodec:h264", "acodec:aac"],
                "merge_output_format": "mp4",
            },
        )
        hasil = _file_hasil(folder)
        if hasil is None:
            raise GagalUnduh("File hasil unduhan tidak ditemukan.")
        if hasil.stat().st_size <= MAX_BYTES:
            return hasil, info
        hasil.unlink()  # kegedean -> ulangi dengan kualitas lebih rendah
    raise GagalUnduh("Video terlalu besar untuk dikirim lewat bot (maks 50 MB).")


# --------------------------------------------------------------- handler


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Halo! 👋\n"
        "Kirim link YouTube atau TikTok, lalu pilih Video (MP4) atau Audio (MP3).\n\n"
        "Batas file bot Telegram 50 MB, jadi kualitas diturunkan otomatis bila perlu "
        f"(video maks {MAX_VIDEO // 60} menit)."
    )


async def terima_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pesan = update.effective_message
    url = cari_url(pesan.text)
    if not url:
        await pesan.reply_text("Kirim link YouTube atau TikTok ya 🙂")
        return
    # callback_data maksimal 64 byte, jadi URL disimpan di memori, kuncinya id pesan
    context.user_data[str(pesan.message_id)] = url
    tombol = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 Video MP4", callback_data=f"v:{pesan.message_id}"),
                InlineKeyboardButton("🎵 Audio MP3", callback_data=f"a:{pesan.message_id}"),
            ]
        ]
    )
    await pesan.reply_text("Pilih format:", reply_markup=tombol)


async def pilih_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    mode, _, kunci = q.data.partition(":")
    url = context.user_data.pop(kunci, None)
    if not url:
        await q.edit_message_text("Link sudah kedaluwarsa, kirim ulang ya.")
        return

    await q.edit_message_text("⏳ Mengunduh, tunggu sebentar...")
    chat_id = update.effective_chat.id
    folder = Path(tempfile.mkdtemp(prefix="dl_"))
    try:
        async with SLOT:
            file, info = await asyncio.to_thread(unduh, url, mode, folder)
        judul = (info.get("title") or "")[:200]
        with open(file, "rb") as f:
            if mode == "a":
                await context.bot.send_audio(
                    chat_id,
                    audio=f,
                    title=judul,
                    performer=info.get("uploader"),
                    duration=info.get("duration"),
                )
            else:
                await context.bot.send_video(
                    chat_id,
                    video=f,
                    caption=judul,
                    duration=info.get("duration"),
                    width=info.get("width"),
                    height=info.get("height"),
                    supports_streaming=True,
                )
        with contextlib.suppress(Exception):
            await q.message.delete()  # hapus pesan "Mengunduh..."
    except GagalUnduh as e:
        await q.edit_message_text(f"❌ {e}")
    except Exception:
        log.exception("Error tak terduga")
        await q.edit_message_text("❌ Terjadi kesalahan. Coba lagi nanti.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # pengguna lain tidak menunggu unduhan orang lain
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(300)  # upload file besar butuh waktu
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(pilih_format, pattern=r"^[va]:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, terima_link
        )
    )
    log.info("Bot berjalan...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
