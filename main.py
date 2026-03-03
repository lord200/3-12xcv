import os
import glob
import logging
import logging.handlers
import subprocess
from datetime import datetime

# Auto-update yt-dlp on every startup
subprocess.run(["pip", "install", "--upgrade", "yt-dlp"], capture_output=True)

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
TIKTOK_COOKIES_FILE = "./tiktok_cookies.txt"
INSTAGRAM_COOKIES_FILE = "./instagram_cookies.txt"

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

# Write TikTok cookies from env var
tiktok_cookies = os.getenv("TIKTOK_COOKIES")
if tiktok_cookies:
    with open(TIKTOK_COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(tiktok_cookies)
    logger.info("✅ TikTok cookies loaded")
else:
    TIKTOK_COOKIES_FILE = None
    logger.warning("⚠️ No TIKTOK_COOKIES env var — age-restricted TikToks may fail")

# Write Instagram cookies from env var
instagram_cookies = os.getenv("INSTAGRAM_COOKIES")
if instagram_cookies:
    with open(INSTAGRAM_COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(instagram_cookies)
    logger.info("✅ Instagram cookies loaded")
else:
    INSTAGRAM_COOKIES_FILE = None
    logger.warning("⚠️ No INSTAGRAM_COOKIES env var — private Instagram content may fail")

# Temporary store: maps user_id -> url
pending_urls: dict[int, str] = {}


# ──────────────────────────────────────────────
# URL DETECTION
# ──────────────────────────────────────────────
def detect_platform(url: str) -> str | None:
    """Returns 'tiktok', 'instagram', or None."""
    if any(d in url for d in ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]):
        return "tiktok"
    if any(d in url for d in ["instagram.com", "instagr.am"]):
        return "instagram"
    return None


def get_ydlp_opts(platform: str) -> dict:
    """Return yt-dlp base options depending on platform."""
    if platform == "tiktok":
        return {
            "quiet": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
            },
            **({"cookiefile": TIKTOK_COOKIES_FILE} if TIKTOK_COOKIES_FILE else {}),
        }
    elif platform == "instagram":
        return {
            "quiet": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.instagram.com/",
            },
            **({"cookiefile": INSTAGRAM_COOKIES_FILE} if INSTAGRAM_COOKIES_FILE else {}),
        }
    return {"quiet": True}


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def get_user_info(update: Update) -> str:
    user = update.effective_user
    return f"@{user.username}" if user.username else f"id:{user.id}"


PLATFORM_EMOJI = {
    "tiktok": "🎵",
    "instagram": "📸",
}


# ──────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_info(update)
    logger.info(f"[START] User {user} started the bot")
    await update.message.reply_text(
        "👋 Welcome! Send me a link and I'll download it for you.\n\n"
        "✅ Supported platforms:\n"
        "🎵 TikTok (videos & audio)\n"
        "📸 Instagram Reels & posts\n\n"
        "Just paste any link!"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = get_user_info(update)
    user_id = update.effective_user.id

    logger.info(f"[REQUEST] User {user} sent URL: {url}")

    platform = detect_platform(url)
    if not platform:
        logger.warning(f"[INVALID URL] User {user} sent unsupported URL: {url}")
        await update.message.reply_text(
            "❌ Unsupported link.\n\n"
            "Please send a TikTok or Instagram Reel link."
        )
        return

    pending_urls[user_id] = url
    emoji = PLATFORM_EMOJI[platform]

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
        f"{emoji} {platform.capitalize()} link detected!\nWhat do you want to download?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = get_user_info(update)
    choice = query.data

    url = pending_urls.pop(user_id, None)
    if not url:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    platform = detect_platform(url)
    base_opts = get_ydlp_opts(platform)

    label = {
        "download_video": "🎬 Video",
        "download_audio": "🎵 Audio",
        "download_both": "📦 Both"
    }[choice]

    logger.info(f"[CHOICE] User {user} chose: {label} | Platform: {platform} | URL: {url}")
    await query.edit_message_text(f"⏳ Downloading {label}... please wait.")

    video_file = None
    audio_file = None
    info = None
    start_time = datetime.now()

    try:
        # --- Fetch metadata ---
        logger.debug(f"[INFO] Fetching metadata | platform={platform} | user={user}")
        with yt_dlp.YoutubeDL({**base_opts}) as ydl:
            info = ydl.extract_info(url, download=False)
            video_id = info["id"]

        logger.debug(f"[INFO] video_id={video_id} | title={info.get('title', 'N/A')}")

        # --- Download Video ---
        if choice in ("download_video", "download_both"):
            logger.debug(f"[DOWNLOAD] Video | id={video_id} | user={user}")
            video_opts = {
                **base_opts,
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s_video.%(ext)s"),
                "format": "mp4/bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            }
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                ydl.extract_info(url, download=True)
                video_file = ydl.prepare_filename(info).replace(
                    f".{info.get('ext', 'mp4')}", ".mp4"
                )

            # Fallback glob
            if not os.path.exists(video_file):
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}_video.*"))
                video_file = matches[0] if matches else None

            if not video_file or not os.path.exists(video_file):
                raise FileNotFoundError("Video file not found after download.")

            video_size_mb = os.path.getsize(video_file) / (1024 * 1024)
            logger.info(f"[VIDEO OK] id={video_id} | size={video_size_mb:.2f}MB | user={user}")

        # --- Download Audio ---
        if choice in ("download_audio", "download_both"):
            logger.debug(f"[DOWNLOAD] Audio | id={video_id} | user={user}")
            audio_opts = {
                **base_opts,
                "outtmpl": os.path.join(DOWNLOAD_DIR, f"{video_id}_audio.%(ext)s"),
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
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
        if video_file and os.path.exists(video_file):
            logger.debug(f"[SEND] Video → {user}")
            with open(video_file, "rb") as vf:
                await query.message.reply_video(
                    video=vf,
                    caption=f"🎬 *{info.get('title', 'Video')}*",
                    parse_mode="Markdown"
                )

        # --- Send Audio ---
        if audio_file and os.path.exists(audio_file):
            logger.debug(f"[SEND] Audio → {user}")
            with open(audio_file, "rb") as af:
                await query.message.reply_audio(
                    audio=af,
                    title=info.get("title", "Audio"),
                    performer=info.get("uploader", platform.capitalize()),
                    caption="🎵 Audio (MP3)"
                )

        elapsed = (datetime.now() - start_time).seconds
        logger.info(f"[SUCCESS] {label} → {user} | took={elapsed}s")

    except FileNotFoundError as e:
        logger.error(f"[FILE ERROR] User={user} | {e}", exc_info=True)
        await query.edit_message_text(f"❌ Failed: {str(e)}")

    except Exception as e:
        logger.error(f"[FAILED] User={user} | URL={url} | {e}", exc_info=True)
        await query.edit_message_text(f"❌ Failed to download.\nError: {str(e)}")

    finally:
        cleaned = []
        for f in [video_file, audio_file]:
            if f and os.path.exists(f):
                os.remove(f)
                cleaned.append(f)
        if cleaned:
            logger.debug(f"[CLEANUP] Removed: {cleaned}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    logger.info("=" * 50)
    logger.info("🤖 Bot starting up...")
    logger.info(f"📁 Downloads     : {os.path.abspath(DOWNLOAD_DIR)}")
    logger.info(f"📋 Logs          : {os.path.abspath(LOGS_DIR)}")
    logger.info(f"🍪 TikTok cookies: {'enabled' if TIKTOK_COOKIES_FILE else 'disabled'}")
    logger.info(f"🍪 IG cookies    : {'enabled' if INSTAGRAM_COOKIES_FILE else 'disabled'}")
    logger.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_choice))

    logger.info("✅ Bot is polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
