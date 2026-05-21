import logging
import os
import tempfile
import aiohttp
import aiofiles
from telegram import Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8961241257:AAEMYfpFC3XBccytnwkivvLnrWWVNpwyjBI"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the command /start is issued."""
    welcome_text = (
        "Welcome! 🎬\n\n"
        "Send me any public TikTok link, and I will extract the media for you without watermarks."
    )
    await update.message.reply_text(welcome_text)

async def fetch_tiktok_data(tiktok_url: str) -> dict:
    """Fetch direct media URLs from the TikWM API."""
    api_url = "https://www.tikwm.com/api/"
    data = {
        "url": tiktok_url,
        "hd": 1
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, data=data) as response:
            response.raise_for_status()
            return await response.json()

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process incoming URLs, fetch data, and show the choice menu."""
    url = update.message.text

    if "tiktok.com" not in url:
        await update.message.reply_text("Please send a valid TikTok link. ❌")
        return

    status_message = await update.message.reply_text("Fetching data... 🔍")

    try:
        api_response = await fetch_tiktok_data(url)
        
        if api_response.get("code") != 0:
            error_msg = api_response.get("msg", "Unknown error")
            await status_message.edit_text(f"Could not extract video. ({error_msg}) 🛑")
            return
            
        data = api_response["data"]
        
        # Save the fetched data to the user's session using their message ID as a unique key
        msg_id = str(update.message.message_id)
        if 'tiktok_data' not in context.user_data:
            context.user_data['tiktok_data'] = {}
            
        context.user_data['tiktok_data'][msg_id] = data

        # Create the interactive buttons
        keyboard = [
            [
                InlineKeyboardButton("🎥 Video / Slideshow", callback_data=f"vid_{msg_id}"),
                InlineKeyboardButton("🎵 Audio Only", callback_data=f"aud_{msg_id}")
            ],
            [InlineKeyboardButton("📦 Send Both", callback_data=f"both_{msg_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Ask the user what they want
        await status_message.edit_text(
            "Data fetched successfully! What would you like to download?", 
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Error processing {url}: {e}")
        await status_message.edit_text("An error occurred while fetching data. Please try again later. 🛑")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button clicks and send the requested media."""
    query = update.callback_query
    await query.answer() # Tell Telegram we received the click

    # Extract the user's choice and the message ID
    choice, msg_id = query.data.split('_', 1)
    
    # Retrieve the saved TikTok data
    data = context.user_data.get('tiktok_data', {}).get(msg_id)
    if not data:
        await query.edit_message_text("Session expired or data lost. Please send the link again. 🛑")
        return

    await query.edit_message_text("Processing your request... ⏳")
    
    title = data.get("title", "TikTok")
    caption_text = title[:900] + "..." if len(title) > 900 else title

    try:
        # --- PART 1: VISUAL CONTENT (SLIDESHOW OR VIDEO) ---
        if choice in ["vid", "both"]:
            if "images" in data:
                await query.edit_message_text("Processing photo slideshow... 📸")
                image_urls = data["images"]
                
                # Slicing images into chunks of 10 to respect Telegram limits
                chunk_size = 10
                for i in range(0, len(image_urls), chunk_size):
                    chunk = image_urls[i:i + chunk_size]
                    media_group = []
                    
                    for j, img_url in enumerate(chunk):
                        # Add caption only to the very first image
                        caption = caption_text if i == 0 and j == 0 else None
                        media_group.append(InputMediaPhoto(media=img_url, caption=caption))
                    
                    await query.message.reply_media_group(
                        media=media_group,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60
                    )
            else:
                video_url = data.get("hdplay") or data.get("play")
                await query.edit_message_text("Downloading high-quality video... ⏳")
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_video:
                    video_filepath = temp_video.name
                    
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(video_url) as response:
                            response.raise_for_status()
                            async with aiofiles.open(video_filepath, 'wb') as f:
                                async for chunk in response.content.iter_chunked(8192):
                                    await f.write(chunk)
                    
                    await query.edit_message_text("Uploading video... 🚀")
                    with open(video_filepath, 'rb') as video_file:
                        await query.message.reply_video(
                            video=video_file, 
                            caption=f"🎬 {caption_text}",
                            supports_streaming=True,
                            read_timeout=120, 
                            write_timeout=120, 
                            connect_timeout=60
                        )
                finally:
                    if os.path.exists(video_filepath):
                        os.remove(video_filepath)

        # --- PART 2: AUDIO CONTENT ---
        if choice in ["aud", "both"]:
            music_url = data.get("music")
            if music_url:
                await query.edit_message_text("Extracting audio track... 🎵")
                
                music_info = data.get("music_info", {})
                music_title = music_info.get("title", "Original Sound")
                music_author = music_info.get("author", "TikTok User")

                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio:
                    audio_filepath = temp_audio.name
                    
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(music_url) as response:
                            response.raise_for_status()
                            async with aiofiles.open(audio_filepath, 'wb') as f:
                                async for chunk in response.content.iter_chunked(8192):
                                    await f.write(chunk)
                    
                    await query.edit_message_text("Uploading audio... 🎵")
                    with open(audio_filepath, 'rb') as audio_file:
                        await query.message.reply_audio(
                            audio=audio_file, 
                            title=music_title, 
                            performer=music_author,
                            read_timeout=120, 
                            write_timeout=120, 
                            connect_timeout=60
                        )
                finally:
                    if os.path.exists(audio_filepath):
                        os.remove(audio_filepath)
            else:
                if choice == "aud":
                    await query.message.reply_text("No audio track found for this video. 🛑")

        # Clean up: delete the loading message and clear memory to prevent leaks
        await query.message.delete()
        if msg_id in context.user_data.get('tiktok_data', {}):
            del context.user_data['tiktok_data'][msg_id]

    except Exception as e:
        logger.error(f"Error in callback processing: {e}")
        await query.edit_message_text("An error occurred while uploading. Please try again. 🛑")

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()