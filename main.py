import os
import re
import json
import glob
import logging
import logging.handlers
import subprocess
import urllib.request
from datetime import datetime

# Auto-update yt-dlp on every startup
subprocess.run(["pip", "install", "--upgrade", "yt-dlp", "requests"], capture_output=True)

import requests
import static_ffmpeg
from dotenv import load_dotenv
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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

# Write TikTok cookies
tiktok_cookies = os.getenv("TIKTOK_COOKIES")
if tiktok_cookies:
    with open(TIKTOK_COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(tiktok_cookies)
    logger.info("✅ TikTok cookies loaded")
else:
    TIKTOK_COOKIES_FILE = None
    logger.warning("⚠️ No TIKTOK_COOKIES — age-restricted TikToks may fail")

# Write Instagram cookies
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
pending_info: dict[int, dict] = {}


# ──────────────────────────────────────────────
# TIKTOK PHOTO POST SCRAPER
# ──────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}


def resolve_redirect(url: str) -> str:
    """Follow redirects and return the final URL."""
    try:
        resp = requests.head(url, headers=BROWSER_HEADERS, allow_redirects=True, timeout=10)
        return resp.url
    except Exception:
        return url


def is_tiktok_photo_url(url: str) -> bool:
    """Check if the URL (after redirect) is a TikTok /photo/ post."""
    return "/photo/" in url


def scrape_tiktok_photos(url: str) -> dict:
    """
    Scrape TikTok photo post page and extract image URLs + metadata.
    Returns: { "images": [...], "title": "...", "uploader": "...", "video_id": "..." }
    """
    # Extract video ID from URL
    match = re.search(r"/photo/(\d+)", url)
    video_id = match.group(1) if match else "unknown"

    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
    html = resp.text

    image_urls = []
    title = "TikTok Photo Post"
    uploader = "TikTok"

    # TikTok embeds all page data in a script tag as JSON
    # Try __UNIVERSAL_DATA_FOR_REHYDRATION__
    pattern = r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            # Navigate the nested structure to find image data
            default_scope = data.get("__DEFAULT_SCOPE__", {})
            video_detail = default_scope.get("webapp.video-detail", {})
            item_info = video_detail.get("itemInfo", {})
            item_struct = item_info.get("itemStruct", {})

            # Get uploader
            author = item_struct.get("author", {})
            uploader = author.get("nickname") or author.get("uniqueId") or "TikTok"

            # Get title/description
            title = item_struct.get("desc", "TikTok Photo Post") or "TikTok Photo Post"

            # Get images from imagePost
            image_post = item_struct.get("imagePost", {})
            images = image_post.get("images", [])
            for img in images:
                img_display = img.get("imageURL", {})
                url_list = img_display.get("urlList", [])
                if url_list:
                    image_urls.append(url_list[0])  # Use first/best URL

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"[SCRAPE] Failed to parse __UNIVERSAL_DATA__: {e}")

    # Fallback: try SIGI_STATE
    if not image_urls:
        pattern2 = r'<script id="SIGI_STATE"[^>]*>(.*?)</script>'
        match2 = re.search(pattern2, html, re.DOTALL)
        if match2:
            try:
                data2 = json.loads(match2.group(1))
                item_module = data2.get("ItemModule", {})
                for item_id, item in item_module.items():
                    image_post = item.get("imagePost", {})
                    images = image_post.get("images", [])
                    for img in images:
                        url_list = img.get("imageURL", {}).get("urlList", [])
                        if url_list:
                            image_urls.append(url_list[0])
                    if images:
                        title = item.get("desc", title)
                        uploader = item.get("author", uploader)
                        break
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"[SCRAPE] Failed to parse SIGI_STATE: {e}")

    return {
        "images": image_urls,
        "title": title,
        "uploader": uploader,
        "video_id": video_id,
    }


def download_photos_to_disk(image_urls: list, video_id: str) -> list:
    """Download image URLs to disk and return list of local file paths."""
    photo_dir = os.path.join(DOWNLOAD_DIR, video_id)
    os.makedirs(photo_dir, exist_ok=True)

    paths = []
    for i, img_url in enumerate(image_urls):
        dest = os.path.join(photo_dir, f"{i+1:03d}.jpg")
        try:
            req = urllib.request.Request(img_url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as response:
                with open(dest, "wb") as f:
                    f.write(response.read())
            paths.append(dest)
            logger.debug(f"[PHOTO] Downloaded {i+1}/{len(image_urls)}")
        except Exception as e:
            logger.warning(f"[PHOTO] Failed to download image {i+1}: {e}")

    return paths


# ──────────────────────────────────────────────
# FRIENDLY ERROR PARSER
# ──────────────────────────────────────────────
def parse_friendly_error(error: Exception, platform: str) -> str:
    msg = str(error).lower()

    if any(k in msg for k in ["private", "login", "log in", "authentication",
                               "not comfortable", "this post may not be", "sign in"]):
        return "🔒 This content is *private* or requires a login.\n\nMake sure your cookies are up to date."
    if any(k in msg for k in ["not available in your country", "geo", "blocked in"]):
        return "🌍 This content is *not available* in the server's region (geo-blocked)."
    if any(k in msg for k in ["removed", "deleted", "no longer available", "does not exist", "404", "not found"]):
        return "🗑️ This content has been *deleted* or no longer exists."
    if any(k in msg for k in ["copyright", "terms of service", "violated"]):
        return "⚠️ This content was *taken down* due to copyright or Terms of Service."
    if any(k in msg for k in ["age", "18+", "adult", "mature"]):
        return "🔞 This content is *age-restricted*.\n\nAdd your cookies to access it."
    if any(k in msg for k in ["no video formats", "no formats found", "unsupported url"]):
        return (
            "📭 This content format is not supported or no downloadable formats were found.\n\n"
            "The content may be private, deleted, or temporarily blocked."
        )
    if any(k in msg for k in ["too large", "file size", "maximum"]):
        return "📦 This file is *too large* to send via Telegram (50MB limit)."
    if any(k in msg for k in ["timeout", "connection", "network", "ssl", "http error"]):
        return "🌐 A *network error* occurred. Please try again."
    if platform == "youtube":
        if "video unavailable" in msg:
            return "❌ This YouTube video is *unavailable* (private, deleted, or region-locked)."
        if "members only" in msg:
            return "👥 This is a *members-only* video."
        if "premiere" in msg:
            return "🎬 This is a *Premiere* that hasn't aired yet."
    if platform == "instagram" and "story" in msg:
        return "📖 Instagram *Stories* are not supported."

    return (
        f"❌ Failed to download this {platform.capitalize()} content.\n\n"
        "The content may be private, deleted, or temporarily unavailable."
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
            "http_headers": BROWSER_HEADERS,
            **({"cookiefile": TIKTOK_COOKIES_FILE} if TIKTOK_COOKIES_FILE else {}),
        }
    if platform == "instagram":
        return {
            **base,
            "http_headers": {**BROWSER_HEADERS, "Referer": "https://www.instagram.com/"},
            **({"cookiefile": INSTAGRAM_COOKIES_FILE} if INSTAGRAM_COOKIES_FILE else {}),
        }
    if platform == "youtube":
        return {**base, "http_headers": BROWSER_HEADERS}
    return base


PLATFORM_EMOJI = {"tiktok": "🎵", "instagram": "📸", "youtube": "▶️"}


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
        await update.message.reply_text(
            "❌ Unsupported link.\n\nSupported: TikTok, Instagram Reels, YouTube"
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        # ── Resolve redirect to detect /photo/ URLs before calling yt-dlp ──
        resolved_url = resolve_redirect(url)
        logger.debug(f"[REDIRECT] {url} → {resolved_url}")

        # ── TikTok photo post — bypass yt-dlp entirely ──
        if platform == "tiktok" and is_tiktok_photo_url(resolved_url):
            logger.info(f"[PHOTO POST] Detected TikTok photo URL | user={user}")
            photo_data = scrape_tiktok_photos(resolved_url)
            photo_count = len(photo_data["images"])

            if photo_count == 0:
                await update.message.reply_text(
                    "😕 Couldn't extract photos from this TikTok post.\n"
                    "The post may be private or the format is not supported."
                )
                return

            # Store scraped data for callback
            pending_urls[user_id] = resolved_url
            pending_info[user_id] = {
                "type": "tiktok_photo",
                "images": photo_data["images"],
                "title": photo_data["title"],
                "uploader": photo_data["uploader"],
                "video_id": photo_data["video_id"],
            }

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
            return

        # ── Normal yt-dlp flow ──
        base_opts = get_ydlp_opts(platform)
        with yt_dlp.YoutubeDL({**base_opts}) as ydl:
            info = ydl.extract_info(resolved_url, download=False)

        pending_urls[user_id] = resolved_url
        pending_info[user_id] = {"type": "media", **info}

        emoji = PLATFORM_EMOJI[platform]

        if platform == "youtube":
            keyboard = [[InlineKeyboardButton("🎵 Download MP3", callback_data="download_audio")]]
            await update.message.reply_text(
                f"{emoji} YouTube link detected!\nYouTube only supports MP3 audio download.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [
                [
                    InlineKeyboardButton("🎬 Video", callback_data="download_video"),
                    InlineKeyboardButton("🎵 Audio (MP3)", callback_data="download_audio"),
                ],
                [InlineKeyboardButton("📦 Both", callback_data="download_both")]
            ]
            await update.message.reply_text(
                f"{emoji} {platform.capitalize()} link detected!\nWhat do you want to download?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        logger.error(f"[METADATA ERROR] User={user} | URL={url} | {e}", exc_info=True)
        await update.message.reply_text(parse_friendly_error(e, platform), parse_mode="Markdown")


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = get_user_info(update)
    choice = query.data

    url = pending_urls.pop(user_id, None)
    cached = pending_info.pop(user_id, None)

    if not url or not cached:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    platform = detect_platform(url)
    content_type = cached.get("type", "media")

    label = {
        "download_video": "🎬 Video",
        "download_audio": "🎵 Audio",
        "download_both": "📦 Both",
        "download_photos": "🖼️ Photos",
        "download_photos_audio": "🖼️ Photos + Audio",
    }[choice]

    logger.info(f"[CHOICE] User {user} | {label} | platform={platform} | type={content_type}")
    await query.edit_message_text(f"⏳ Downloading {label}... please wait.")

    video_file = None
    audio_file = None
    photo_files = []
    video_id = cached.get("video_id") or cached.get("id", "unknown")
    title = cached.get("title", "Unknown")
    uploader = cached.get("uploader") or cached.get("channel") or platform.capitalize()
    duration_sec = cached.get("duration", 0)

    start_time = datetime.now()

    try:
        base_opts = get_ydlp_opts(platform)

        # ── TIKTOK PHOTO POST ──
        if content_type == "tiktok_photo":
            if choice in ("download_photos", "download_photos_audio"):
                image_urls = cached.get("images", [])
                logger.debug(f"[DOWNLOAD] {len(image_urls)} photos | user={user}")
                photo_files = download_photos_to_disk(image_urls, video_id)
                if not photo_files:
                    raise RuntimeError("No photos could be downloaded.")
                logger.info(f"[PHOTOS OK] count={len(photo_files)} | user={user}")

            if choice in ("download_audio", "download_photos_audio"):
                logger.debug(f"[DOWNLOAD] Audio for photo post | user={user}")
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
                    raise FileNotFoundError("MP3 not found after conversion.")

                logger.info(f"[AUDIO OK] size={os.path.getsize(audio_file)/(1024*1024):.2f}MB | user={user}")

        # ── NORMAL VIDEO/AUDIO ──
        else:
            info = cached  # full yt-dlp info dict

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

                logger.info(f"[VIDEO OK] size={os.path.getsize(video_file)/(1024*1024):.2f}MB | user={user}")

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
                    raise FileNotFoundError("MP3 not found after conversion.")

                logger.info(f"[AUDIO OK] size={os.path.getsize(audio_file)/(1024*1024):.2f}MB | user={user}")

        await query.edit_message_text("✅ Done! Sending your file(s)...")

        # ── Send Photos ──
        if photo_files:
            logger.debug(f"[SEND] {len(photo_files)} photos → {user}")
            CHUNK = 10
            for i in range(0, len(photo_files), CHUNK):
                chunk = photo_files[i:i + CHUNK]
                media_group = []
                handles = []
                for j, path in enumerate(chunk):
                    fh = open(path, "rb")
                    handles.append(fh)
                    cap = f"🖼️ *{title}* ({i+j+1}/{len(photo_files)})" if j == 0 else None
                    media_group.append(InputMediaPhoto(media=fh, caption=cap, parse_mode="Markdown"))
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

    except Exception as e:
        logger.error(f"[FAILED] User={user} | platform={platform} | {e}", exc_info=True)
        await query.edit_message_text(parse_friendly_error(e, platform), parse_mode="Markdown")

    finally:
        for f in [video_file, audio_file]:
            if f and os.path.exists(f):
                os.remove(f)

        if photo_files:
            photo_dir = os.path.join(DOWNLOAD_DIR, video_id)
            for f in photo_files:
                if os.path.exists(f):
                    os.remove(f)
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
