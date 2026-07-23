import os
import json
import uuid
import shutil
import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from urllib.request import urlopen, Request

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    daily_limit: int
    max_concurrent_downloads: int
    ytdlp_cookies_file: str

def load_config() -> Config:
    raw_admins = os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    admin_ids = {int(x) for x in raw_admins if x.isdigit()}

    return Config(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        admin_ids=admin_ids,
        daily_limit=int(os.environ.get("DAILY_LIMIT", "10")),
        max_concurrent_downloads=int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3")),
        ytdlp_cookies_file=os.environ.get("YTDLP_COOKIES_FILE", "").strip(),
    )

CONFIG = load_config()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("downloader_bot")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class BotState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.download_semaphore = asyncio.Semaphore(CONFIG.max_concurrent_downloads)
        self.stats = {"users": {}, "total_downloads": 0}

STATE = BotState()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in CONFIG.admin_ids

def get_user_record(user_id: int):
    uid = str(user_id)
    return STATE.stats["users"].setdefault(uid, {"count": 0, "vip": False, "daily_count": 0, "daily_date": ""})

def check_daily_limit(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    u = get_user_record(user_id)
    if u.get("vip"):
        return True
    today = date.today().isoformat()
    if u.get("daily_date") != today:
        u["daily_date"] = today
        u["daily_count"] = 0
    return u.get("daily_count", 0) < CONFIG.daily_limit

def increment_daily_count(user_id: int):
    u = get_user_record(user_id)
    today = date.today().isoformat()
    if u.get("daily_date") != today:
        u["daily_date"] = today
        u["daily_count"] = 0
    u["daily_count"] += 1

# ---------------------------------------------------------------------------
# دانلود اصلی (بهینه برای تیک‌تاک + اینستاگرام)
# ---------------------------------------------------------------------------
async def download_media(status_msg, user, url: str):
    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": f"{job_dir}/%(id)s.%(ext)s",
        "quiet": True,
        "noplaylist": False,
        "merge_output_format": "mp4",
        "format": "bestvideo+bestaudio/best",   # برای ویدیوهای تیک‌تاک بهتره
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "extractor_args": {
            "TikTok": {"webpage": True},
            "instagram": {"player_client": ["ios", "android"]},
        },
    }

    if CONFIG.ytdlp_cookies_file and os.path.exists(CONFIG.ytdlp_cookies_file):
        ydl_opts["cookiefile"] = CONFIG.ytdlp_cookies_file

    try:
        await status_msg.edit_text("⬇️ در حال دانلود ویدیو...")

        loop = asyncio.get_running_loop()
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await loop.run_in_executor(None, _download)

        files = [os.path.join(job_dir, f) for f in os.listdir(job_dir) if os.path.isfile(os.path.join(job_dir, f))]

        sent = False
        for fname in files:
            lower = fname.lower()
            if lower.endswith(('.mp4', '.mov')):
                await status_msg.reply_video(video=open(fname, "rb"), supports_streaming=True)
                sent = True
            elif lower.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                await status_msg.reply_photo(photo=open(fname, "rb"))
                sent = True

        if not sent and files:
            await status_msg.reply_document(open(files[0], "rb"))

        await status_msg.reply_text("🙏 با تشکر از امپراطور فانی")
        increment_daily_count(user.id)

    except Exception as e:
        logger.exception("TikTok/Instagram Download Error")
        await status_msg.edit_text(f"خطا در دانلود: {str(e)[:180]}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or update.message.caption or "").strip()

    # استخراج لینک‌های تیک‌تاک و اینستاگرام
    urls = [u for u in text.split() if any(domain in u.lower() for domain in ["tiktok.com", "instagram.com"])]

    if not urls:
        await update.message.reply_text("لینک تیک‌تاک یا اینستاگرام بفرست.")
        return

    for url in urls:
        status_msg = await update.message.reply_text("در حال پردازش لینک...")
        await download_media(status_msg, user, url)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام 👋\nلینک ویدیو یا پست تیک‌تاک یا اینستاگرام رو بفرست.",
        reply_markup=ReplyKeyboardMarkup([["📥 دانلود"]], resize_keyboard=True)
    )

def main():
    app = ApplicationBuilder().token(CONFIG.bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & \~filters.COMMAND, handle_links))
    print("ربات دانلود تیک‌تاک + اینستاگرام آماده است...")
    app.run_polling()

if __name__ == "__main__":
    main()
