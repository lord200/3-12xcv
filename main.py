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
from telegram.constants import ChatAction

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

logger = logging.getLogger("DownloaderBot")
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
    logger.warning("⚠️ No TIKTOK_COOKIES — age-restricted TikToks may fail")

# Write Instagram cookies from env var
instagram_cookies = os.getenv("INSTAGRAM_COOKIES")
if instagram_cookies:
    with open(INSTAGRAM_COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(instagram_cookies)
    logger.info("✅ Instagram cookies loaded")
else:
    INSTAGRAM_COOKIES_FILE = None
    logger.warning("⚠️ No INSTAGRAM_COOKIES — private Instagram content may fail")

# Temporary stores
pending_urls: dict[int, str] = {}
pending_info: dict[int, dict] = {}  # stores pre-fetched yt-dlp info per user


# ──────────────────────────────────────────────
# FRIENDLY ERROR PARSER
# ──────────────────────────────────────────────
def parse_friendly_error(error: Exception, platform: str) -> str:
    msg = str(error).lower()

    if any(k in msg for k in ["private", "login", "log in", "authentication",
                               "not comfortable", "this post may not be", "sign in"]):
        return (
            "🔒 This content is *private* or requires a login to access.\n\n"
            "Make sure your cookies are up to date."
        )
    if any(k in msg for k in ["not available in your country", "geo", "blocked in"]):
        return "🌍 This content is *not available* in the server's region (geo-blocked)."
    if any(k in msg for k in ["removed", "deleted", "no longer available", "does not exist", "404", "not found"]):
        return "🗑️ This content has been *deleted* or no longer exists."
    if any(k in msg for k in ["copyright", "terms of service", "violated"]):
        return "⚠️ This content was *taken down* due to copyright or Terms of Service."
    if any(k in msg for k in ["age", "18+", "adult", "mature"]):
        return (
            "🔞 This content is *age-restricted*.\n\n"
            "Add your cookies to the bot to access it."
        )
    if any(k in msg for k in ["no video formats", "no formats found"]):
        return (
            "📭 No downloadable formats were found.\n\n"
            "The content may be private, deleted, or temporarily blocked.\n"
            "Try again in a moment."
        )
    if any(k in msg for k in ["too large", "file size", "maximum"]):
        return "📦 This file is *too large* to send via Telegram (50MB limit)."
    if any(k in msg for k in ["timeout", "connection", "network", "ssl", "http error"]):
        return "🌐 A *network error* occurred. Please try again."
    if platform == "youtube":
        if "video unavailable" in msg:
            return "❌ This YouTube video is *unavailable* (private, deleted, or region-locked)."
        if "members only" in msg:
            return "👥 This is a *members-only* YouTube video and cannot be downloaded."
        if "premiere" in msg:
            return "🎬 This YouTube video is a *Premiere* and hasn't aired yet."
    if platform == "instagram":
        if "story" in msg:
            return "📖 Instagram *Stories* are not supported."

    return (
        f"❌ Failed to download this {platform.capitalize()} content.\n\n"
        "Possible reasons: the content is private, deleted, or temporarily unavailable.\n"
        "Please try again later."
    )


# ──────────────────────────────────────────────
# PLATFORM DETECTION
# ──────────────────────────────────────────────
def detect_platform(url: str) -> str | None:
    if any(d in url for d in ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]):
        return "tiktok"
    if any(d in url for d in ["instagram.com", "instagr.am"]):
        return "instagram"
    if any(d in url for d in ["youtube.com", "youtu.be", "youtube-nocookie.com"]):
        return "youtube"
    return None


def get_ydlp_opts(platform: str) -> dict:
    base = {"quiet": True}
    if platform == "tiktok":
        return {
            **base,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
            },
            **({"cookiefile": TIKTOK_COOKIES_FILE} if TIKTOK_COOKIES_FILE else {}),
        }
    if platform == "instagram":
        return {
            **base,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.instagram.com/",
            },
            **({"cookiefile": INSTAGRAM_COOKIES_FILE} if INSTAGRAM_COOKIES_FILE else {}),
        }
    if platform == "youtube":
        return {
            **base,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        }
    return base


def is_tiktok_slideshow(info: dict) -> bool:
    """Detect if a TikTok is a photo slideshow instead of a video."""
    # TikTok slideshows have images in the 'images' key or entries with image formats
    if info.get("images"):
        return True
    formats = info.get("formats", [])
    # If all formats are images (no video stream), it's a slideshow
    if formats and all(
        f.get("vcodec") == "none" and f.get("ext") in ("jpg", "jpeg", "png", "webp")
        for f in formats if f.get("ext") in ("jpg", "jpeg", "png", "webp")
    ):
        return True
    # Check _type or direct image URLs
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = info["entries"]
        if entries and all(
            e.get("ext") in ("jpg", "jpeg", "png", "webp") for e in entries if e
        ):
            return True
    return False


PLATFORM_EMOJI = {
    "tiktok": "🎵",
    "instagram": "📸",
    "youtube": "▶️",
}


def get_user_info(update: Update) -> str:
    user = update.effective_user
    return f"@{user.username}" if user.username else f"id:{user.id}"


# ──────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_info(update)
    logger.info(f"[START] User {user} started the bot")
    await update.message.reply_text(
        "👋 Welcome! Send me a link and I'll download it.\n\n"
        "✅ Supported:\n"
        "🎵 TikTok — video, audio & photo slideshows\n"
        "📸 Instagram — Reels & posts\n"
        "▶️ YouTube — MP3 audio only\n\n"
        "Just paste any link!"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = get_user_info(update)
    user_id = update.effective_user.id

    logger.info(f"[REQUEST] User {user} | URL: {url}")

    platform = detect_platform(url)
    if not platform:
        logger.warning(f"[INVALID URL] User {user} | URL: {url}")
        await update.message.reply_text(
            "❌ Unsupported link.\n\nSupported: TikTok, Instagram Reels, YouTube"
        )
        return

    # Show typing indicator while we fetch metadata
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        base_opts = get_ydlp_opts(platform)
        with yt_dlp.YoutubeDL({**base_opts}) as ydl:
            info = ydl.extract_info(url, download=False)

        # Cache info and url for callback use
        pending_urls[user_id] = url
        pending_info[user_id] = info

        emoji = PLATFORM_EMOJI[platform]

        # ── TikTok slideshow detected ──
        if platform == "tiktok" and is_tiktok_slideshow(info):
            photo_count = len(info.get("images", [])) or len(info.get("entries", [])) or "?"
            logger.info(f"[SLIDESHOW] Detected TikTok slideshow | photos={photo_count} | user={user}")
            keyboard = [
                [
                    InlineKeyboardButton(f"🖼️ Photos ({photo_count})", callback_data="download_photos"),
                    InlineKeyboardButton("🎵 Audio (MP3)", callback_data="download_audio"),
                ],
                [
                    InlineKeyboardButton("📦 Photos + Audio", callback_data="download_photos_audio"),
                ]
            ]
            await update.message.reply_text(
                f"🖼️ TikTok *photo slideshow* detected! ({photo_count} photos)\n\n"
                "How do you want to download it?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        # ── YouTube ──
        elif platform == "youtube":
            keyboard = [
                [InlineKeyboardButton("🎵 Download MP3", callback_data="download_audio")]
            ]
            await update.message.reply_text(
                f"{emoji} YouTube link detected!\nYouTube only supports MP3 audio download.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # ── TikTok video / Instagram ──
        else:
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

    except Exception as e:
        logger.error(f"[METADATA ERROR] User={user} | URL={url} | {e}", exc_info=True)
        friendly = parse_friendly_error(e, platform)
        await update.message.reply_text(friendly, parse_mode="Markdown")


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = get_user_info(update)
    choice = query.data

    url = pending_urls.pop(user_id, None)
    info = pending_info.pop(user_id, None)

    if not url or not info:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    platform = detect_platform(url)
    base_opts = get_ydlp_opts(platform)
    video_id = info["id"]
    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel") or platform.capitalize()
    duration_sec = info.get("duration", 0)

    label = {
        "download_video": "🎬 Video",
        "download_audio": "🎵 Audio",
        "download_both": "📦 Both",
        "download_photos": "🖼️ Photos",
        "download_photos_audio": "🖼️ Photos + Audio",
    }[choice]

    logger.info(f"[CHOICE] User {user} | {label} | platform={platform}")
    await query.edit_message_text(f"⏳ Downloading {label}... please wait.")

    video_file = None
    audio_file = None
    photo_files = []
    start_time = datetime.now()

    try:
        # ── SLIDESHOW: Download photos ──
        if choice in ("download_photos", "download_photos_audio"):
            logger.debug(f"[DOWNLOAD] Slideshow photos | id={video_id} | user={user}")

            photo_dir = os.path.join(DOWNLOAD_DIR, video_id)
            os.makedirs(photo_dir, exist_ok=True)

            photo_opts = {
                **base_opts,
                "outtmpl": os.path.join(photo_dir, "%(autonumber)s.%(ext)s"),
                "format": "mhtml/best",          # TikTok slideshow format
                "write_pages": False,
            }

            # yt-dlp stores slideshow images in info["images"]
            images = info.get("images", [])
            if images:
                import urllib.request
                for i, img in enumerate(images):
                    img_url = img.get("url") if isinstance(img, dict) else img
                    if img_url:
                        ext = "jpg"
                        dest = os.path.join(photo_dir, f"{i+1:03d}.{ext}")
                        urllib.request.urlretrieve(img_url, dest)
                        if os.path.exists(dest):
                            photo_files.append(dest)
            else:
                # Fallback: let yt-dlp download them
                with yt_dlp.YoutubeDL(photo_opts) as ydl:
                    ydl.extract_info(url, download=True)
                photo_files = sorted(glob.glob(os.path.join(photo_dir, "*.*")))
                photo_files = [f for f in photo_files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]

            logger.info(f"[PHOTOS OK] id={video_id} | count={len(photo_files)} | user={user}")

        # ── Download Audio ──
        if choice in ("download_audio", "download_both", "download_photos_audio"):
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

        # ── Download Video ──
        if choice in ("download_video", "download_both"):
            if platform == "youtube":
                await query.edit_message_text("⚠️ YouTube only supports MP3 audio download.")
                return

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

            if not os.path.exists(video_file):
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}_video.*"))
                video_file = matches[0] if matches else None

            if not video_file or not os.path.exists(video_file):
                raise FileNotFoundError("Video file not found after download.")

            video_size_mb = os.path.getsize(video_file) / (1024 * 1024)
            logger.info(f"[VIDEO OK] id={video_id} | size={video_size_mb:.2f}MB | user={user}")

        await query.edit_message_text("✅ Done! Sending your file(s)...")

        # ── Send Photos as media group ──
        if photo_files:
            logger.debug(f"[SEND] {len(photo_files)} photos → {user}")
            from telegram import InputMediaPhoto

            # Telegram allows max 10 per media group
            CHUNK_SIZE = 10
            for i in range(0, len(photo_files), CHUNK_SIZE):
                chunk = photo_files[i:i + CHUNK_SIZE]
                media_group = []
                handles = []
                for j, path in enumerate(chunk):
                    fh = open(path, "rb")
                    handles.append(fh)
                    caption = f"🖼️ *{title}* ({i+j+1}/{len(photo_files)})" if j == 0 else None
                    media_group.append(InputMediaPhoto(media=fh, caption=caption, parse_mode="Markdown"))

                await query.message.reply_media_group(media=media_group)

                for fh in handles:
                    fh.close()

        # ── Send Video ──
        if video_file and os.path.exists(video_file):
            logger.debug(f"[SEND] Video → {user}")
            with open(video_file, "rb") as vf:
                await query.message.reply_video(
                    video=vf,
                    caption=f"🎬 *{title}*",
                    parse_mode="Markdown"
                )

        # ── Send Audio ──
        if audio_file and os.path.exists(audio_file):
            logger.debug(f"[SEND] Audio → {user}")
            with open(audio_file, "rb") as af:
                await query.message.reply_audio(
                    audio=af,
                    title=title,
                    performer=uploader,
                    duration=duration_sec,
                    caption="🎵 Audio (MP3)"
                )

        elapsed = (datetime.now() - start_time).seconds
        logger.info(f"[SUCCESS] {label} → {user} | platform={platform} | took={elapsed}s")

    except FileNotFoundError as e:
        logger.error(f"[FILE ERROR] User={user} | {e}", exc_info=True)
        await query.edit_message_text(parse_friendly_error(e, platform), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[FAILED] User={user} | platform={platform} | URL={url} | {e}", exc_info=True)
        await query.edit_message_text(parse_friendly_error(e, platform), parse_mode="Markdown")

    finally:
        # Cleanup single files
        for f in [video_file, audio_file]:
            if f and os.path.exists(f):
                os.remove(f)

        # Cleanup photo directory
        if photo_files:
            photo_dir = os.path.join(DOWNLOAD_DIR, video_id)
            for f in photo_files:
                if os.path.exists(f):
                    os.remove(f)
            if os.path.exists(photo_dir):
                try:
                    os.rmdir(photo_dir)
                except OSError:
                    pass

        logger.debug(f"[CLEANUP] Done for id={video_id}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    logger.info("=" * 50)
    logger.info("🤖 Downloader Bot starting up...")
    logger.info(f"📁 Downloads        : {os.path.abspath(DOWNLOAD_DIR)}")
    logger.info(f"📋 Logs             : {os.path.abspath(LOGS_DIR)}")
    logger.info(f"🍪 TikTok cookies   : {'enabled' if TIKTOK_COOKIES_FILE else 'disabled'}")
    logger.info(f"🍪 Instagram cookies: {'enabled' if INSTAGRAM_COOKIES_FILE else 'disabled'}")
    logger.info("▶️  YouTube          : MP3 audio only")
    logger.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_choice))

    logger.info("✅ Bot is polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
