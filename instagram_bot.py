import os
import json
import uuid
import asyncio
import logging
from urllib.parse import urlparse
from cachetools import TTLCache

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatMemberStatus
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# تنظیمات پایه
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("متغیر BOT_TOKEN در Railway تنظیم نشده است.")

ADMIN_IDS = set()
raw_admins = os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
for x in raw_admins:
    if x.isdigit():
        ADMIN_IDS.add(int(x))

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "").strip()
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

STATS_FILE = "stats.json"
stats_lock = asyncio.Lock()

url_cache = TTLCache(maxsize=1000, ttl=3600)

SUPPORTED_DOMAINS = [
    "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "facebook.com", "fb.watch"
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")

# ---------------------------------------------------------------------------
# دیتابیس ساده JSON
# ---------------------------------------------------------------------------
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("total_downloads", 0)
                data.setdefault("total_errors", 0)
                data.setdefault("users", {})
                data.setdefault("banned", [])
                data.setdefault("maintenance", False)
                return data
        except Exception:
            pass
    return {"total_downloads": 0, "total_errors": 0, "users": {}, "banned": [], "maintenance": False}

def save_stats(data):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

stats = load_stats()

async def record_user(user_id: int, username: str):
    async with stats_lock:
        uid = str(user_id)
        if uid not in stats["users"]:
            stats["users"][uid] = {"count": 0, "username": username or ""}
        elif username:
            stats["users"][uid]["username"] = username
        save_stats(stats)

async def record_download(user_id: int):
    async with stats_lock:
        stats["total_downloads"] += 1
        uid = str(user_id)
        if uid in stats["users"]:
            stats["users"][uid]["count"] += 1
        save_stats(stats)

async def record_error():
    async with stats_lock:
        stats["total_errors"] += 1
        save_stats(stats)

# ---------------------------------------------------------------------------
# بررسی عضویت کانال
# ---------------------------------------------------------------------------
async def is_channel_member(bot, user_id: int) -> bool:
    if not CHANNEL_USERNAME or user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return True

# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------
def quality_keyboard(short_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 بهترین کیفیت (ویدیو)", callback_data=f"q:best:{short_id}")],
        [InlineKeyboardButton("📺 کیفیت متوسط (720p)", callback_data=f"q:720:{short_id}")],
        [InlineKeyboardButton("🎧 فقط صدا (MP3)", callback_data=f"q:audio:{short_id}")],
        [InlineKeyboardButton("❌ انصراف", callback_data="cancel_select")]
    ])

def force_join_keyboard():
    ch_clean = CHANNEL_USERNAME.replace("@", "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{ch_clean}")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join")]
    ])

# ---------------------------------------------------------------------------
# دستورات عمومی
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in stats.get("banned", []):
        await update.message.reply_text("🚫 شما مسدود شده‌اید.")
        return

    await update.message.reply_text(
        f"سلام {user.first_name or 'دوست عزیز'} 👋\n\n"
        "🔗 لینک پست یا استوری اینستاگرام، یا لینک یوتیوب، تیک‌تاک، توییتر و فیسبوک رو برام بفرست تا برات دانلود کنم!"
    )

# ---------------------------------------------------------------------------
# دستورات ادمین
# ---------------------------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(f"⛔️ دسترسی غیرمجاز!\nآیدی تلگرام شما: `{user_id}`", parse_mode=ParseMode.MARKDOWN)
        return

    text = (
        "📊 *پنل مدیریت ربات*\n\n"
        f"👥 تعداد کاربران: {len(stats['users'])}\n"
        f"📥 دانلودهای موفق: {stats['total_downloads']}\n"
        f"❌ مجموع خطاها: {stats['total_errors']}\n"
        f"🛠 حالت تعمیرات: {'فعال 🛠' if stats.get('maintenance') else 'غیرفعال 🟢'}\n"
        f"🚫 کاربران مسدود: {len(stats.get('banned', []))}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(f"⛔️ دسترسی غیرمجاز!\nآیدی تلگرام شما: `{user_id}`", parse_mode=ParseMode.MARKDOWN)
        return

    msg_to_send = update.message.reply_to_message
    text = " ".join(context.args) if context.args else ""

    if not msg_to_send and not text:
        await update.message.reply_text(
            "⚠️ *نحوه استفاده از دستور همگانی:*\n\n"
            "۱️⃣ *پیام متنی:* `/broadcast سلام به همه`\n"
            "۲️⃣ *عکس/ویدیو:* یک عکس بفرستید، روی آن ریپلای کرده و بنویسید `/broadcast`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    status_msg = await update.message.reply_text("🚀 در حال ارسال پیام همگانی...")
    success, failed = 0, 0

    for uid_str in list(stats["users"].keys()):
        try:
            uid = int(uid_str)
            if msg_to_send:
                await msg_to_send.copy(chat_id=uid)
            else:
                await context.bot.send_message(chat_id=uid, text=text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"📊 *گزارش ارسال همگانی:*\n\n✅ موفق: {success}\n❌ ناموفق: {failed}",
        parse_mode=ParseMode.MARKDOWN
    )

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        if uid not in stats["banned"]:
            stats["banned"].append(uid)
            save_stats(stats)
        await update.message.reply_text(f"🚫 کاربر {uid} مسدود شد.")
    else:
        await update.message.reply_text("استفاده: `/ban USER_ID`", parse_mode=ParseMode.MARKDOWN)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        if uid in stats["banned"]:
            stats["banned"].remove(uid)
            save_stats(stats)
        await update.message.reply_text(f"✅ کاربر {uid} آزاد شد.")
    else:
        await update.message.reply_text("استفاده: `/unban USER_ID`", parse_mode=ParseMode.MARKDOWN)

async def toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    stats["maintenance"] = not stats.get("maintenance", False)
    save_stats(stats)
    await update.message.reply_text(f"⚙️ حالت تعمیرات: {'فعال 🛠' if stats['maintenance'] else 'غیرفعال 🟢'}")

# ---------------------------------------------------------------------------
# دریافت و پردازش لینک
# ---------------------------------------------------------------------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in stats.get("banned", []):
        return
    if stats.get("maintenance", False) and user.id not in ADMIN_IDS:
        await update.message.reply_text("🛠 ربات در حال تعمیرات است.")
        return

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text("⚠️ ابتدا در کانال ما عضو شوید:", reply_markup=force_join_keyboard())
        return

    url = update.message.text.strip()
    if "?" in url and ("tiktok.com" in url or "instagram.com" in url):
        url = url.split("?")[0]

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    if not parsed.scheme.startswith("http") or not any(d in domain for d in SUPPORTED_DOMAINS):
        await update.message.reply_text("🚫 لطفاً یک لینک معتبر بفرستید.")
        return

    short_id = uuid.uuid4().hex[:8]
    url_cache[short_id] = url
    await update.message.reply_text("🎚 کیفیت مورد نظرت رو انتخاب کن:", reply_markup=quality_keyboard(short_id))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "check_join":
        if await is_channel_member(context.bot, user.id):
            await query.message.delete()
            await query.message.reply_text("✅ عضویت تایید شد! حالا لینک بفرستید.")
        else:
            await query.answer("❌ هنوز عضو نشده‌اید!", show_alert=True)
        return

    if data == "cancel_select":
        await query.message.delete()
        return

    if data.startswith("q:"):
        _, quality, short_id = data.split(":", 2)
        url = url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، دوباره بفرستید.")
            return
        await query.message.delete()
        asyncio.create_task(download_and_send(query.message, user, url, quality))

# ---------------------------------------------------------------------------
# موتور اصلی دانلود
# ---------------------------------------------------------------------------
async def download_and_send(chat_msg, user, url, quality):
    status_msg = await chat_msg.reply_text("⏳ در حال دانلود... لطفاً شکیبا باشید.")
    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ydl_opts = {
        'outtmpl': f'{job_dir}/%(id)s_%(autonumber)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'user_agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
        # کمک به دور زدن محدودیت‌های اخیر یوتیوب
        'extractor_args': {
            'youtube': {'player_client': ['android', 'web']}
        },
    }

    if quality == "audio":
        # برای صدا حتماً باید تبدیل به mp3 انجام بشه، وگرنه فایل خام (وبم/اپوس) دانلود می‌شه
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif quality == "720":
        ydl_opts['format'] = 'best[height<=720]/best'
    # برای quality == "best" فرمت رو مشخص نمی‌کنیم تا پست‌های عکسی/اسلایدشو
    # (مثل کاروسل تیک‌تاک) هم درست دانلود بشن، نه فقط ویدیو

    loop = asyncio.get_event_loop()
    try:
        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await loop.run_in_executor(None, _extract)
        if not info:
            raise RuntimeError("اطلاعاتی دریافت نشد. ممکنه لینک خصوصی یا نامعتبر باشه.")

        files = [os.path.join(job_dir, f) for f in os.listdir(job_dir) if os.path.isfile(os.path.join(job_dir, f))]
        if not files:
            raise RuntimeError("فایلی دانلود نشد.")

        await status_msg.edit_text("📤 در حال ارسال فایل...")

        for fname in files:
            lower = fname.lower()
            with open(fname, "rb") as f:
                if lower.endswith(IMAGE_EXTS):
                    await chat_msg.reply_photo(photo=f, caption="✅ دانلود شد!")
                elif lower.endswith(AUDIO_EXTS):
                    await chat_msg.reply_audio(audio=f, caption="✅ فایل صوتی دانلود شد!")
                else:
                    await chat_msg.reply_video(video=f, caption="✅ ویدیو دانلود شد!")

        await status_msg.delete()
        await record_download(user.id)

    except Exception as e:
        logger.exception("خطا در دانلود")
        await record_error()
        err_text = str(e)[:300]
        await status_msg.edit_text(f"❌ خطا در دانلود:\n`{err_text}`", parse_mode=ParseMode.MARKDOWN)

    finally:
        for f in os.listdir(job_dir):
            try:
                os.remove(os.path.join(job_dir, f))
            except Exception:
                pass
        try:
            os.rmdir(job_dir)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# اجرای اصلی
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("off", toggle_maintenance))
    app.add_handler(CommandHandler("on", toggle_maintenance))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    print("ربات روشن شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
