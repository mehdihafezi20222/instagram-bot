import os
import re
import json
import time
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
# تنظیمات پایه و متغیرهای محیطی
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

# آیدی‌های ادمین (جداشده با کاما)
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

# آیدی یا یوزرنیم کانال جهت عضویت اجباری (مثال: @my_channel)
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "").strip()

# تنظیمات کوکی یوتیوب جهت دور زدن لیمیت سرور
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "").strip()
COOKIE_FILE_PATH = "/tmp/cookies.txt"
if YOUTUBE_COOKIES:
    try:
        with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(YOUTUBE_COOKIES)
    except Exception as e:
        logger.error(f"خطا در ساخت فایل کوکی: {e}")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

STATS_FILE = "stats.json"
stats_lock = asyncio.Lock()

RATE_LIMIT_SECONDS = 20
last_request_time = {}  # user_id -> timestamp

# کش کردن لینک‌ها برای ۱ ساعت جهت مدیریت RAM سرور
url_cache = TTLCache(maxsize=1000, ttl=3600)
MAX_CAPTION_LEN = 500

MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))

SUPPORTED_DOMAINS = [
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
]


# ---------------------------------------------------------------------------
# آمار و ذخیره‌سازی داده‌ها
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
            logger.exception("خطا در خواندن فایل آمار")
    return {
        "total_downloads": 0,
        "total_errors": 0,
        "users": {},
        "banned": [],
        "maintenance": False,
    }


def save_stats(data):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("خطا در ذخیره فایل آمار")


stats = load_stats()


async def record_user(user_id: int, username: str):
    async with stats_lock:
        uid = str(user_id)
        if uid not in stats["users"]:
            stats["users"][uid] = {"count": 0, "username": username or ""}
        elif username:
            stats["users"][uid]["username"] = username
        save_stats(stats)


async def record_download(user_id: int, username: str):
    async with stats_lock:
        stats["total_downloads"] += 1
        uid = str(user_id)
        u = stats["users"].setdefault(uid, {"count": 0, "username": username or ""})
        u["count"] += 1
        if username:
            u["username"] = username
        save_stats(stats)


async def record_error():
    async with stats_lock:
        stats["total_errors"] += 1
        save_stats(stats)


# ---------------------------------------------------------------------------
# بررسی عضویت در کانال
# ---------------------------------------------------------------------------
async def is_channel_member(bot, user_id: int) -> bool:
    if not CHANNEL_USERNAME or user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ]
    except Exception as e:
        logger.warning(f"خطا در بررسی عضویت کانال: {e}")
        return True


# ---------------------------------------------------------------------------
# صف دانلود
# ---------------------------------------------------------------------------
download_queue: "asyncio.Queue" = asyncio.Queue()
active_jobs = {}


class DownloadCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------
def main_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📖 راهنما", callback_data="help"),
                InlineKeyboardButton("ℹ️ درباره ربات", callback_data="about"),
            ],
            [InlineKeyboardButton("💬 پشتیبانی", url="https://t.me/")],
        ]
    )


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back")]])


def force_join_keyboard():
    ch_clean = CHANNEL_USERNAME.replace("@", "")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{ch_clean}")],
            [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join")],
        ]
    )


def redownload_keyboard(short_id: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 دانلود مجدد", callback_data=f"redl:{short_id}")]]
    )


def quality_keyboard(short_id: str):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎬 بهترین کیفیت", callback_data=f"q:best:{short_id}")],
            [InlineKeyboardButton("📺 کیفیت متوسط (720p)", callback_data=f"q:720:{short_id}")],
            [InlineKeyboardButton("🎧 فقط صدا (MP3)", callback_data=f"q:audio:{short_id}")],
            [InlineKeyboardButton("❌ انصراف", callback_data="cancel_select")],
        ]
    )


def cancel_keyboard(job_id: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⛔️ لغو دانلود", callback_data=f"cancel:{job_id}")]]
    )


# ---------------------------------------------------------------------------
# دستورات کاربران
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in stats.get("banned", []):
        await update.message.reply_text("🚫 شما از استفاده از این ربات مسدود شده‌اید.")
        return

    if stats.get("maintenance", False) and user.id not in ADMIN_IDS:
        await update.message.reply_text("🛠 ربات در حال حاضر در دست تعمیرات است. لطفاً بعداً مراجعه کنید.")
        return

    text = (
        f"✨ *سلام {user.first_name or 'دوست عزیز'} 👋، به ربات دانلودر خوش اومدی!*\n\n"
        "🔗 لینک پست، ریلز یا استوری اینستاگرام، یا لینک یوتیوب، توییتر/X، تیک‌تاک و فیسبوک "
        "رو برام بفرست تا برات دانلودش کنم.\n\n"
        "👇 برای شروع یکی از گزینه‌های زیر رو انتخاب کن یا مستقیم لینک بفرست:"
    )
    await update.message.reply_text(
        text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN
    )


# ---------------------------------------------------------------------------
# دستورات مدیریتی ادمین
# ---------------------------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔️ دسترسی غیرمجاز!\nآیدی عددی شما (`{user_id}`) در لیست ادمین‌ها ست نشده است.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    total_users = len(stats["users"])
    banned_count = len(stats.get("banned", []))
    maint_status = "فعال 🛠" if stats.get("maintenance", False) else "غیرفعال 🟢"
    ch_status = CHANNEL_USERNAME if CHANNEL_USERNAME else "تنظیم نشده ❌"

    text = (
        "📊 *پنل مدیریت ربات*\n\n"
        f"👥 تعداد کل کاربران: {total_users}\n"
        f"📥 مجموع دانلودهای موفق: {stats['total_downloads']}\n"
        f"❌ مجموع خطاها: {stats['total_errors']}\n"
        f"⏳ اندازه صف فعلی: {download_queue.qsize()}\n"
        f"🔄 دانلودهای درحال اجرا: {len(active_jobs)}\n"
        f"🛠 حالت تعمیرات: {maint_status}\n"
        f"🚫 کاربران مسدود شده: {banned_count}\n"
        f"📢 کانال عضویت اجباری: {ch_status}\n"
    )

    top = sorted(stats["users"].items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]
    if top:
        text += "\n🏆 *پرتکرارترین کاربران:*\n"
        for uid, info in top:
            label = f"@{info['username']}" if info.get("username") else uid
            text += f"  • {label}: {info['count']} دانلود\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔️ دسترسی غیرمجاز!\nآیدی عددی شما (`{user_id}`) در لیست ادمین‌ها ست نشده است.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg_to_send = update.message.reply_to_message
    text = " ".join(context.args)

    if not msg_to_send and not text:
        await update.message.reply_text(
            "⚠️ *راهنمای استفاده از دستور همگانی:*\n\n"
            "۱️⃣ **پیام متنی:** `/broadcast متن پیام شما`\n"
            "۲️⃣ **عکس/ویدیو/فایل:** عکس یا فیلمی ارسال کنید، سپس روی آن ریپلای کرده و بنویسید `/broadcast`",
            parse_mode=ParseMode.MARKDOWN,
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
            await asyncio.sleep(0.04)  # رعایت لیمیت ارسال پیام تلگرام
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"📊 *گزارش ارسال همگانی:*\n\n"
        f"✅ ارسال موفق: {success}\n"
        f"❌ ناموفق / بلاک‌شده: {failed}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ فرمت صحیح: `/ban 12345678`", parse_mode=ParseMode.MARKDOWN)
        return

    uid = int(context.args[0])
    async with stats_lock:
        if uid not in stats["banned"]:
            stats["banned"].append(uid)
            save_stats(stats)
    await update.message.reply_text(f"🚫 کاربر `{uid}` با موفقیت مسدود شد.", parse_mode=ParseMode.MARKDOWN)


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ فرمت صحیح: `/unban 12345678`", parse_mode=ParseMode.MARKDOWN)
        return

    uid = int(context.args[0])
    async with stats_lock:
        if uid in stats["banned"]:
            stats["banned"].remove(uid)
            save_stats(stats)
    await update.message.reply_text(f"✅ کاربر `{uid}` از مسدودی خارج شد.", parse_mode=ParseMode.MARKDOWN)


async def toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    async with stats_lock:
        stats["maintenance"] = not stats.get("maintenance", False)
        status = "فعال 🛠" if stats["maintenance"] else "غیرفعال 🟢"
        save_stats(stats)

    await update.message.reply_text(f"⚙️ وضعیت تعمیرات تغییر کرد:\nوضعیت جدید: *{status}*", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# پردازش دکمه‌ها
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if user.id in stats.get("banned", []):
        await query.message.reply_text("🚫 شما مسدود هستید.")
        return

    if data == "check_join":
        if await is_channel_member(context.bot, user.id):
            await query.message.delete()
            await query.message.reply_text("✅ عضویت شما تایید شد! حالا می‌تونید لینکتون رو بفرستید.")
        else:
            await query.answer("❌ شما هنوز در کانال عضو نشده‌اید!", show_alert=True)
        return

    if data == "help":
        text = (
            "📖 *راهنمای استفاده*\n\n"
            "۱️⃣ لینک پست/ریلز/استوری اینستاگرام یا لینک یوتیوب، توییتر/X، تیک‌تاک و فیسبوک رو کپی کن\n"
            "۲️⃣ همینجا برام بفرستش\n"
            "۳️⃣ کیفیت موردنظرت رو انتخاب کن\n"
            "۴️⃣ اگه صف شلوغ بود منتظر می‌مونی، وگرنه فوراً دانلود می‌شه 🎬\n\n"
            f"⏱ بین هر درخواست حداقل {RATE_LIMIT_SECONDS} ثانیه فاصله لازمه.\n"
            f"📦 حداکثر حجم مجاز هر فایل: {MAX_FILE_SIZE_MB} مگابایت."
        )
        await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode=ParseMode.MARKDOWN)

    elif data == "about":
        text = (
            "ℹ️ *درباره ربات*\n\n"
            "🐍 ساخته‌شده با Python و کتابخانه‌ی yt-dlp\n"
            "📥 دانلود محتوای عمومی از اینستاگرام، یوتیوب، توییتر/X، تیک‌تاک و فیسبوک\n"
            "🚀 سریع، ساده و رایگان"
        )
        await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode=ParseMode.MARKDOWN)

    elif data == "back":
        text = (
            f"✨ *سلام {user.first_name or 'دوست عزیز'} 👋*\n\n"
            "🔗 لینک موردنظرت رو برام بفرست:"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)

    elif data == "cancel_select":
        await query.message.delete()

    elif data.startswith("q:"):
        _, quality, short_id = data.split(":", 2)
        url = url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، لطفاً دوباره لینک رو بفرست.")
            return
        await query.message.delete()
        await enqueue_download(query.message, query.from_user, url, quality)

    elif data.startswith("redl:"):
        short_id = data.split(":", 1)[1]
        url = url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، لطفاً دوباره لینک رو بفرست.")
            return
        new_short_id = uuid.uuid4().hex[:8]
        url_cache[new_short_id] = url
        await query.message.reply_text(
            "🎚 کیفیت موردنظرت رو انتخاب کن:", reply_markup=quality_keyboard(new_short_id)
        )

    elif data.startswith("cancel:"):
        job_id = data.split(":", 1)[1]
        job = active_jobs.get(job_id)
        if job:
            job["cancel"] = True
        else:
            await query.answer("این دانلود دیگه فعال نیست.", show_alert=False)


# ---------------------------------------------------------------------------
# محدودیت نرخ درخواست
# ---------------------------------------------------------------------------
def check_rate_limit(user_id: int):
    now = time.time()
    last = last_request_time.get(user_id, 0)
    elapsed = now - last
    if elapsed < RATE_LIMIT_SECONDS:
        return False, int(RATE_LIMIT_SECONDS - elapsed)
    last_request_time[user_id] = now
    return True, 0


# ---------------------------------------------------------------------------
# دریافت لینک
# ---------------------------------------------------------------------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in stats.get("banned", []):
        await update.message.reply_text("🚫 شما مسدود شده‌اید.")
        return

    if stats.get("maintenance", False) and user.id not in ADMIN_IDS:
        await update.message.reply_text("🛠 ربات در حال تعمیرات است.")
        return

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "⚠️ برای استفاده از ربات، ابتدا باید در کانال عضو شوید:",
            reply_markup=force_join_keyboard(),
        )
        return

    url = update.message.text.strip()

    # حذف پارامترهای اضافی تیک‌تاک
    if "tiktok.com" in url and "?" in url:
        url = url.split("?")[0]

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    if not parsed.scheme.startswith("http") or not any(d in domain for d in SUPPORTED_DOMAINS):
        await update.message.reply_text(
            "🚫 لطفاً یک لینک معتبر از اینستاگرام، یوتیوب، توییتر/X، تیک‌تاک یا فیسبوک بفرست.",
            reply_markup=main_menu_keyboard(),
        )
        return

    allowed, wait_left = check_rate_limit(user.id)
    if not allowed and user.id not in ADMIN_IDS:
        await update.message.reply_text(
            f"⏱ خیلی سریع درخواست دادی! لطفاً {wait_left} ثانیه دیگه دوباره امتحان کن."
        )
        return

    short_id = uuid.uuid4().hex[:8]
    url_cache[short_id] = url

    await update.message.reply_text(
        "🎚 کیفیت موردنظرت رو انتخاب کن:\n"
        "(اگه لینک، پست چندعکسی/کاروسل باشه، همه‌ی آیتم‌ها ارسال می‌شن)",
        reply_markup=quality_keyboard(short_id),
    )


# ---------------------------------------------------------------------------
# افزودن به صف و پردازش دانلود
# ---------------------------------------------------------------------------
async def enqueue_download(chat_message, user, url, quality):
    job_id = uuid.uuid4().hex[:10]
    job = {
        "job_id": job_id,
        "url": url,
        "user_id": user.id,
        "username": user.username or "",
        "chat_message": chat_message,
        "quality": quality,
    }
    position_ahead = download_queue.qsize()
    await download_queue.put(job)

    if position_ahead > 0:
        await chat_message.reply_text(
            f"⏳ درخواست شما به صف اضافه شد. تعداد درخواست‌های جلوتر از شما: {position_ahead}"
        )
    else:
        await chat_message.reply_text("⏳ درخواست شما ثبت شد، به‌زودی شروع می‌شه...")


async def safe_edit_status(status_msg, text, parse_mode=None, reply_markup=None):
    try:
        await status_msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        pass


async def process_job(job):
    job_id = job["job_id"]
    url = job["url"]
    user_id = job["user_id"]
    username = job["username"]
    chat_message = job["chat_message"]
    quality = job.get("quality", "best")

    status_msg = await chat_message.reply_text(
        "⏳ در حال دانلود... ۰٪", reply_markup=cancel_keyboard(job_id)
    )
    active_jobs[job_id] = {"cancel": False, "status_msg": status_msg}

    loop = asyncio.get_event_loop()
    last_reported = {"percent": -1, "time": time.time()}

    def progress_hook(d):
        if active_jobs.get(job_id, {}).get("cancel"):
            raise DownloadCancelled("لغو شد توسط کاربر")
        if d.get("status") != "downloading":
            return
        try:
            percent_str = d.get("_percent_str", "0%").strip().replace("%", "")
            percent = int(float(percent_str))
        except (ValueError, TypeError):
            return

        current_time = time.time()
        if (percent - last_reported["percent"] >= 15) and (current_time - last_reported["time"] >= 2.5):
            last_reported["percent"] = percent
            last_reported["time"] = current_time
            asyncio.run_coroutine_threadsafe(
                safe_edit_status(
                    status_msg,
                    f"⏳ در حال دانلود... {percent}٪",
                    reply_markup=cancel_keyboard(job_id),
                ),
                loop,
            )

    format_map = {
        "best": "best",
        "720": "best[height<=720]/best",
        "audio": "bestaudio/best",
    }
    fmt = format_map.get(quality, "best")

    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": f"{job_dir}/%(id)s.%(ext)s",
        "format": fmt,
        "quiet": True,
        "noplaylist": False,
        "progress_hooks": [progress_hook],
    }

    if os.path.exists(COOKIE_FILE_PATH):
        ydl_opts["cookiefile"] = COOKIE_FILE_PATH

    if quality == "audio":
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    downloaded_files = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, True))
            entries = info.get("entries") if info.get("_type") == "playlist" else [info]

            for entry in entries:
                if entry is None:
                    continue
                fname = ydl.prepare_filename(entry)
                if quality == "audio":
                    fname = os.path.splitext(fname)[0] + ".mp3"
                if os.path.exists(fname):
                    downloaded_files.append(fname)

        if not downloaded_files:
            raise RuntimeError("هیچ فایلی برای دانلود پیدا نشد.")

        await safe_edit_status(status_msg, "📤 در حال ارسال فایل...")

        original_caption = (info.get("description") or "").strip()
        if len(original_caption) > MAX_CAPTION_LEN:
            original_caption = original_caption[:MAX_CAPTION_LEN].rstrip() + "…"

        short_id = uuid.uuid4().hex[:8]
        url_cache[short_id] = url
        markup = redownload_keyboard(short_id)

        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        total = len(downloaded_files)
        sent_any = False

        for idx, fname in enumerate(downloaded_files, start=1):
            size = os.path.getsize(fname)
            if size > max_bytes:
                await chat_message.reply_text(
                    f"⚠️ فایل شماره {idx} از {total} حجمش حدود {size // (1024 * 1024)} مگابایته "
                    f"و از حد مجاز ({MAX_FILE_SIZE_MB} مگابایت) بیشتره."
                )
                continue

            caption_parts = []
            if not sent_any:
                caption_parts.append("✅ دانلود با موفقیت انجام شد!")
                if original_caption:
                    caption_parts.append(f"\n📝 کپشن اصلی:\n{original_caption}")
            if total > 1:
                caption_parts.append(f"\n({idx}/{total})")
            caption_parts.append("\n🙏 با تشکر از استفاده شما")
            caption = "\n".join(caption_parts)

            is_last = idx == total
            lower = fname.lower()
            with open(fname, "rb") as f:
                if lower.endswith(".mp3"):
                    await chat_message.reply_audio(
                        audio=f, caption=caption, reply_markup=markup if is_last else None
                    )
                elif lower.endswith((".mp4", ".mov", ".webm")):
                    await chat_message.reply_video(
                        video=f, caption=caption, reply_markup=markup if is_last else None
                    )
                else:
                    await chat_message.reply_photo(
                        photo=f, caption=caption, reply_markup=markup if is_last else None
                    )
            sent_any = True

        await status_msg.delete()
        await record_download(user_id, username)

    except DownloadCancelled:
        await safe_edit_status(status_msg, "⛔️ دانلود توسط شما لغو شد.")

    except Exception as e:
        logger.exception("خطا در دانلود")
        await record_error()
        await safe_edit_status(status_msg, f"❌ خطا در دانلود:\n`{e}`", parse_mode=ParseMode.MARKDOWN)

    finally:
        active_jobs.pop(job_id, None)
        for fname in downloaded_files:
            try:
                if os.path.exists(fname):
                    os.remove(fname)
            except Exception:
                pass
        try:
            if os.path.isdir(job_dir) and not os.listdir(job_dir):
                os.rmdir(job_dir)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ورکرهای صف و اجرای اصلی
# ---------------------------------------------------------------------------
async def queue_worker():
    while True:
        job = await download_queue.get()
        try:
            await process_job(job)
        except Exception:
            logger.exception("خطای غیرمنتظره در پردازش صف")
        finally:
            download_queue.task_done()


async def post_init(app):
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        asyncio.create_task(queue_worker())


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # دستورات عمومی
    app.add_handler(CommandHandler("start", start))

    # دستورات مدیریت (با پشتیبانی از متن و کپشن رسانه برای همگانی)
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("broadcast", broadcast, filters=filters.TEXT | filters.CAPTION))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("off", toggle_maintenance))
    app.add_handler(CommandHandler("on", toggle_maintenance))

    # هندلر دکمه‌ها و لینک‌ها
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    print("ربات با موفقیت روشن شد...")
    app.run_polling()


if __name__ == "__main__":
    main()

