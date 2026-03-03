import os
import glob
import logging
import logging.handlers
from datetime import datetime
import static_ffmpeg
from dotenv import load_dotenv
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

static_ffmpeg.add_paths()
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
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

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

file_handler = logging.handlers.TimedRotatingFileHandler(
    filename=os.path.join(LOGS_DIR, "bot.log"),
    when="midnight", interval=1, backupCount=7, encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

error_handler = logging.FileHandler(
    filename=os.path.join(LOGS_DIR, "errors.log"), encoding="utf-8"
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.addHandler(error_handler)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
# ──────────────────────────────────────────────

# Temporary store: maps user_id -> tiktok_url
pending_urls: dict[int, str] = {}


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url or "vm.tiktok.com" in url


def get_user_info(update: Update) -> str:
    user = update.effective_user
    return f"@{user.username}" if user.username else f"id:{user.id}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_info(update)
    logger.info(f"[START] User {user} started the bot")
    await update.message.reply_text(
        "👋 Welcome! Send me a TikTok link and I'll ask what you want to download."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = get_user_info(update)
    user_id = update.effective_user.id

    logger.info(f"[REQUEST] User {user} sent URL: {url}")

    if not is_tiktok_url(url):
        logger.warning(f"[INVALID URL] User {user} sent non-TikTok URL: {url}")
        await update.message.reply_text("❌ Please send a valid TikTok URL.")
        return

    # Save URL so the callback handler can use it
    pending_urls[user_id] = url

    keyboard = [
        [
            InlineKeyboardButton("🎬 Video", callback_data="download_video"),
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data="download_audio"),
        ],
        [
            InlineKeyboardButton("📦 Both", callback_data="download_both"),
        ]
    ]

    await update.message.reply_text(
        "What do you want to download?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = get_user_info(update)
    choice = query.data  # "download_video" | "download_audio" | "download_both"

    url = pending_urls.pop(user_id, None)

    if not url:
        await query.edit_message_text("❌ Session expired. Please send the TikTok link again.")
        return

    label = {"download_video": "🎬 Video", "download_audio": "🎵 Audio", "download_both": "📦 Both"}[choice]
    logger.info(f"[CHOICE] User {user} chose: {label} | URL: {url}")

    await query.edit_message_text(f"⏳ Downloading {label}... please wait.")

    video_file = None
    audio_file = None
    start_time = datetime.now()

    try:
        video_id = None

        # --- Download Video ---
        if choice in ("download_video", "download_both"):
            logger.debug(f"[DOWNLOAD] Video for {user}")
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

        # --- Download Audio ---
        if choice in ("download_audio", "download_both"):
            logger.debug(f"[DOWNLOAD] Audio for {user}")

            # If video wasn't downloaded, we still need the video_id
            if not video_id:
                with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
                    video_id = info["id"]

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

        await query.edit_message_text("✅ Done! Sending your file(s)...")

        # --- Send Video ---
        if video_file:
            logger.debug(f"[SEND] Sending video to {user}")
            with open(video_file, "rb") as vf:
                await query.message.reply_video(
                    video=vf,
                    caption=f"🎬 *{info.get('title', 'TikTok Video')}*",
                    parse_mode="Markdown"
                )

        # --- Send Audio ---
        if audio_file:
            logger.debug(f"[SEND] Sending audio to {user}")
            with open(audio_file, "rb") as af:
                await query.message.reply_audio(
                    audio=af,
                    title=info.get("title", "TikTok Audio"),
                    performer=info.get("uploader", "TikTok"),
                    caption="🎵 Audio (MP3)"
                )

        elapsed = (datetime.now() - start_time).seconds
        logger.info(f"[SUCCESS] Delivered {label} to {user} | took={elapsed}s")

    except FileNotFoundError as e:
        logger.error(f"[FILE ERROR] User={user} | Error={e}", exc_info=True)
        await query.edit_message_text("❌ Failed: could not find the converted audio file.")

    except Exception as e:
        logger.error(f"[FAILED] User={user} | URL={url} | Error={e}", exc_info=True)
        await query.edit_message_text(f"❌ Failed to download.\nError: {str(e)}")

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_choice))

    logger.info("✅ Bot is polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
