import os
import re
import json
import glob
import logging
import logging.handlers
import subprocess
import urllib.request
import http.cookiejar
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
# BROWSER HEADERS
# ──────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.tiktok.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


# ──────────────────────────────────────────────
# COOKIES HELPER — parse Netscape cookies.txt → requests session
# ──────────────────────────────────────────────
def load_cookies_into_session(session: requests.Session, cookies_file: str):
    """Parse a Netscape cookies.txt file and load into a requests session."""
    if not cookies_file or not os.path.exists(cookies_file):
        return
    try:
        with open(cookies_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, path, secure, expires, name, value = parts[:7]
                cookie = requests.cookies.create_cookie(
                    name=name,
                    value=value,
                    domain=domain.lstrip("."),
                    path=path,
                )
                session.cookies.set_cookie(cookie)
        logger.debug(f"[COOKIES] Loaded {len(session.cookies)} cookies from {cookies_file}")
    except Exception as e:
        logger.warning(f"[COOKIES] Failed to load cookies: {e}")


def make_tiktok_session() -> requests.Session:
    """Create a requests session with TikTok cookies and headers."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    load_cookies_into_session(session, TIKTOK_COOKIES_FILE)
    return session


# ──────────────────────────────────────────────
# TIKTOK PHOTO SCRAPER
# ──────────────────────────────────────────────
def resolve_redirect(url: str) -> str:
    """Follow redirects to get the final URL."""
    try:
        session = make_tiktok_session()
        resp = session.head(url, allow_redirects=True, timeout=10)
        final = resp.url
        logger.debug(f"[REDIRECT] {url} → {final}")
        return final
    except Exception as e:
        logger.warning(f"[REDIRECT] Failed: {e}")
        return url


def is_tiktok_photo_url(url: str) -> bool:
    return "/photo/" in url


def scrape_tiktok_photos(url: str) -> dict:
    """
    Scrape TikTok photo post using authenticated session (with cookies).
    Tries multiple JSON extraction strategies.
    """
    match = re.search(r"/photo/(\d+)", url)
    video_id = match.group(1) if match else "unknown"

    result = {
        "images": [],
        "title": "TikTok Photo Post",
        "uploader": "TikTok",
        "video_id": video_id,
    }

    session = make_tiktok_session()

    try:
        resp = session.get(url, timeout=15, allow_redirects=True)
        html = resp.text
        logger.debug(f"[SCRAPE] Page fetched | status={resp.status_code} | size={len(html)} chars")

        # ── Strategy 1: __UNIVERSAL_DATA_FOR_REHYDRATION__ ──
        match1 = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if match1:
            try:
                data = json.loads(match1.group(1))
                scope = data.get("__DEFAULT_SCOPE__", {})
                item_struct = (
                    scope.get("webapp.video-detail", {})
                         .get("itemInfo", {})
                         .get("itemStruct", {})
                )
                _extract_from_item_struct(item_struct, result)
                if result["images"]:
                    logger.info(f"[SCRAPE] Strategy 1 success | photos={len(result['images'])}")
                    return result
            except Exception as e:
                logger.debug(f"[SCRAPE] Strategy 1 failed: {e}")

        # ── Strategy 2: SIGI_STATE ──
        match2 = re.search(
            r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if match2:
            try:
                data2 = json.loads(match2.group(1))
                for item_id, item in data2.get("ItemModule", {}).items():
                    _extract_from_item_struct(item, result)
                    if result["images"]:
                        logger.info(f"[SCRAPE] Strategy 2 success | photos={len(result['images'])}")
                        return result
            except Exception as e:
                logger.debug(f"[SCRAPE] Strategy 2 failed: {e}")

        # ── Strategy 3: Generic JSON search for imagePost ──
        all_json_blocks = re.findall(r'\{[^{}]*"imagePost"[^{}]*\}', html)
        for block in all_json_blocks:
            try:
                data3 = json.loads(block)
                images = data3.get("imagePost", {}).get("images", [])
                for img in images:
                    url_list = img.get("imageURL", {}).get("urlList", [])
                    if url_list:
                        result["images"].append(url_list[0])
                if result["images"]:
                    logger.info(f"[SCRAPE] Strategy 3 success | photos={len(result['images'])}")
                    return result
            except Exception:
                continue

        # ── Strategy 4: TikTok API with video ID ──
        if video_id != "unknown":
            api_result = _try_tiktok_api(video_id, session)
            if api_result["images"]:
                logger.info(f"[SCRAPE] Strategy 4 (API) success | photos={len(api_result['images'])}")
                return api_result

        logger.warning(f"[SCRAPE] All strategies failed for video_id={video_id}")
        # Log a snippet of the HTML to help debug
        logger.debug(f"[SCRAPE] HTML snippet: {html[:500]}")

    except Exception as e:
        logger.error(f"[SCRAPE] Request failed: {e}", exc_info=True)

    return result


def _extract_from_item_struct(item: dict, result: dict):
    """Extract images, title, uploader from a TikTok itemStruct dict."""
    if not item:
        return

    author = item.get("author", {})
    if isinstance(author, dict):
        result["uploader"] = author.get("nickname") or author.get("uniqueId") or result["uploader"]
    elif isinstance(author, str):
        result["uploader"] = author

    result["title"] = item.get("desc") or result["title"]

    image_post = item.get("imagePost", {})
    images = image_post.get("images", [])
    for img in images:
        if isinstance(img, dict):
            url_list = img.get("imageURL", {}).get("urlList", [])
            if url_list:
                result["images"].append(url_list[0])


def _try_tiktok_api(video_id: str, session: requests.Session) -> dict:
    """Try TikTok's internal API endpoint to get photo data."""
    result = {"images": [], "title": "TikTok Photo Post", "uploader": "TikTok", "video_id": video_id}
    try:
        api_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}&aid=1988"
        resp = session.get(api_url, timeout=10)
        data = resp.json()
        item = data.get("itemInfo", {}).get("itemStruct", {})
        _extract_from_item_struct(item, result)
    except Exception as e:
        logger.debug(f"[API] TikTok API attempt failed: {e}")
    return result


def download_photos_to_disk(image_urls: list, video_id: str) -> list:
    """Download photos using authenticated session."""
    photo_dir = os.path.join(DOWNLOAD_DIR, video_id)
    os.makedirs(photo_dir, exist_ok=True)

    session = make_tiktok_session()
    paths = []

    for i, img_url in enumerate(image_urls):
        dest = os.path.join(photo_dir, f"{i+1:03d}.jpg")
        try:
            resp = session.get(img_url, timeout=15, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
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
        return "📭 No downloadable formats found.\n\nThe content may be private, deleted, or temporarily blocked."
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
        resolved_url = resolve_redirect(url)

        # ── TikTok photo post ──
        if platform == "tiktok" and is_tiktok_photo_url(resolved_url):
            logger.info(f"[PHOTO POST] TikTok photo URL detected | user={user}")
            photo_data = scrape_tiktok_photos(resolved_url)
            photo_count = len(photo_data["images"])

            if photo_count == 0:
                await update.message.reply_text(
                    "😕 Couldn't extract photos from this TikTok post.\n\n"
                    "This usually means your *TikTok cookies* are expired or missing.\n"
                    "Please update the `TIKTOK_COOKIES` variable in Railway and redeploy.",
                    parse_mode="Markdown"
                )
                return

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
                logger.info(f"[AUDIO OK] size={os.path.getsize(audio_file)/(1024*1024):.2f}MB")

        # ── NORMAL VIDEO/AUDIO ──
        else:
            info = cached

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
                logger.info(f"[VIDEO OK] size={os.path.getsize(video_file)/(1024*1024):.2f}MB")

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
                logger.info(f"[AUDIO OK] size={os.path.getsize(audio_file)/(1024*1024):.2f}MB")

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
