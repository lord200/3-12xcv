import os
import glob
import logging
import logging.handlers
from datetime import datetime
from dotenv import load_dotenv
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.critical("❌ BOT_TOKEN is not set! Add it to your .env file.")
    raise ValueError("BOT_TOKEN environment variable is missing.")

DOWNLOAD_DIR = "./downloads"
LOGS_DIR = "./logs"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("TikTokBot")
logger.setLevel(logging.DEBUG)

# 1. Console handler (INFO and above)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

# 2. File handler — rotates daily, keeps 7 days of logs
file_handler = logging.handlers.TimedRotatingFileHandler(
    filename=os.path.join(LOGS_DIR, "bot.log"),
    when="midnight",
    interval=1,
    backupCount=7,
    encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

# 3. Error-only log file
error_handler = logging.FileHandler(
    filename=os.path.join(LOGS_DIR, "errors.log"),
    encoding="utf-8"
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.addHandler(error_handler)

# Silence noisy telegram/httpx internal logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
# ──────────────────────────────────────────────


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url or "vm.tiktok.com" in url


def get_user_info(update: Update) -> str:
    """Return a readable user identifier for logs."""
    user = update.effective_user
    return f"@{user.username}" if user.username else f"id:{user.id}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_info(update)
    logger.info(f"[START] User {user} started the bot")

    await update.message.reply_text(
        "👋 Welcome! Send me a TikTok link and I'll download:\n"
        "🎬 The video\n"
        "🎵 The audio (MP3)\n\n"
        "Just paste any TikTok URL!"
    )


async def download_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = get_user_info(update)

    logger.info(f"[REQUEST] User {user} sent URL: {url}")

    if not is_tiktok_url(url):
        logger.warning(f"[INVALID URL] User {user} sent non-TikTok URL: {url}")
        await update.message.reply_text("❌ Please send a valid TikTok URL.")
        return

    msg = await update.message.reply_text("⏳ Downloading... please wait.")

    video_file = None
    audio_file = None
    start_time = datetime.now()

    try:
        # --- Download Video ---
        logger.debug(f"[DOWNLOAD] Starting video download for {user} | URL: {url}")
        video_opts = {
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s_video.%(ext)s"),
            "format": "mp4",
            "quiet": True,
        }

        with yt_dlp.YoutubeDL(video_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info["id"]
            video_file = ydl.prepare_filename(info)

        video_size_mb = os.path.getsize(video_file) / (1024 * 1024)
        logger.info(f"[VIDEO OK] id={video_id} | size={video_size_mb:.2f}MB | user={user}")

        # --- Download Audio as MP3 ---
        logger.debug(f"[DOWNLOAD] Starting audio extraction for {user} | id={video_id}")
        audio_opts = {
            "outtmpl": os.path.join(DOWNLOAD_DIR, f"{video_id}_audio.%(ext)s"),
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
        }

        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            ydl.extract_info(url, download=True)

        matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}_audio.*"))
        audio_file = next((f for f in matches if f.endswith(".mp3")), None)

        if not audio_file:
            raise FileNotFoundError("MP3 file not found after conversion.")

        audio_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        logger.info(f"[AUDIO OK] id={video_id} | size={audio_size_mb:.2f}MB | user={user}")

        await msg.edit_text("✅ Done! Sending files...")

        # --- Send Video ---
        logger.debug(f"[SEND] Sending video to {user}")
        with open(video_file, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=f"🎬 *{info.get('title', 'TikTok Video')}*",
                parse_mode="Markdown"
            )

        # --- Send Audio ---
        logger.debug(f"[SEND] Sending audio to {user}")
        with open(audio_file, "rb") as af:
            await update.message.reply_audio(
                audio=af,
                title=info.get("title", "TikTok Audio"),
                performer=info.get("uploader", "TikTok"),
                caption="🎵 Audio (MP3)"
            )

        elapsed = (datetime.now() - start_time).seconds
        logger.info(f"[SUCCESS] Delivered to {user} | id={video_id} | took={elapsed}s")

    except FileNotFoundError as e:
        logger.error(f"[FILE ERROR] User={user} | URL={url} | Error={e}", exc_info=True)
        await msg.edit_text("❌ Failed: could not find the converted audio file.")

    except Exception as e:
        logger.error(f"[FAILED] User={user} | URL={url} | Error={e}", exc_info=True)
        await msg.edit_text(f"❌ Failed to download.\nError: {str(e)}")

    finally:
        cleaned = []
        for f in [video_file, audio_file]:
            if f and os.path.exists(f):
                os.remove(f)
                cleaned.append(f)
        if cleaned:
            logger.debug(f"[CLEANUP] Removed files: {cleaned}")


def main():
    logger.info("=" * 50)
    logger.info("🤖 TikTok Bot starting up...")
    logger.info(f"📁 Downloads dir : {os.path.abspath(DOWNLOAD_DIR)}")
    logger.info(f"📋 Logs dir      : {os.path.abspath(LOGS_DIR)}")
    logger.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_tiktok))

    logger.info("✅ Bot is polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()