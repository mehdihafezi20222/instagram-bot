import os
import re
import json
import time
import uuid
import asyncio
import logging
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# تنظیمات پایه
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

# آیدی عددی ادمین‌ها، جدا شده با کاما — مثال: ADMIN_IDS="123456,987654"
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

STATS_FILE = "stats.json"

RATE_LIMIT_SECONDS = 20
last_request_time = {}  # user_id -> timestamp آخرین درخواست

url_cache = {}  # short_id -> url (برای دکمه‌ی انتخاب کیفیت / دانلود مجدد)
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
# آمار و پنل ادمین (ذخیره‌سازی ساده روی فایل JSON)
# ---------------------------------------------------------------------------
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("خطا در خواندن فایل آمار")
    return {"total_downloads": 0, "total_errors": 0, "users": {}}


def save_stats(data):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("خطا در ذخیره فایل آمار")


stats = load_stats()


def record_download(user_id: int, username: str):
    stats["total_downloads"] += 1
    u = stats["users"].setdefault(str(user_id), {"count": 0, "username": username or ""})
    u["count"] += 1
    if username:
        u["username"] = username
    save_stats(stats)


def record_error():
    stats["total_errors"] += 1
    save_stats(stats)


# ---------------------------------------------------------------------------
# صف دانلود
# ---------------------------------------------------------------------------
download_queue: "asyncio.Queue" = asyncio.Queue()
active_jobs = {}  # job_id -> {"cancel": bool, "status_msg": Message}


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
# دستورات ساده
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "دوست عزیز"
    text = (
        f"✨ *سلام {name} 👋، به ربات دانلودر خوش اومدی!*\n\n"
        "🔗 لینک پست، ریلز یا استوری اینستاگرام، یا لینک یوتیوب، توییتر/X، تیک‌تاک و فیسبوک "
        "رو برام بفرست تا برات دانلودش کنم.\n\n"
        "👇 برای شروع یکی از گزینه‌های زیر رو انتخاب کن یا مستقیم لینک بفرست:"
    )
    await update.message.reply_text(
        text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ این دستور فقط برای ادمین‌هاست.")
        return

    total_users = len(stats["users"])
    text = (
        "📊 *پنل آمار ربات*\n\n"
        f"👥 تعداد کاربران: {total_users}\n"
        f"📥 مجموع دانلودهای موفق: {stats['total_downloads']}\n"
        f"❌ مجموع خطاها: {stats['total_errors']}\n"
        f"⏳ اندازه صف فعلی: {download_queue.qsize()}\n"
        f"🔄 دانلودهای درحال اجرا: {len(active_jobs)}\n"
    )

    top = sorted(stats["users"].items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]
    if top:
        text += "\n🏆 *پرتکرارترین کاربران:*\n"
        for uid, info in top:
            label = f"@{info['username']}" if info.get("username") else uid
            text += f"  • {label}: {info['count']} دانلود\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# دکمه‌ها
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "help":
        text = (
            "📖 *راهنمای استفاده*\n\n"
            "۱️⃣ لینک پست/ریلز/استوری اینستاگرام یا لینک یوتیوب، توییتر/X، تیک‌تاک و فیسبوک رو کپی کن\n"
            "۲️⃣ همینجا برام بفرستش\n"
            "۳️⃣ کیفیت موردنظرت رو انتخاب کن\n"
            "۴️⃣ اگه صف شلوغ بود منتظر می‌مونی، وگرنه فوراً دانلود می‌شه 🎬\n"
            "۵️⃣ هر موقع خواستی می‌تونی دانلود در حال انجام رو لغو کنی\n\n"
            f"⏱ بین هر درخواست حداقل {RATE_LIMIT_SECONDS} ثانیه فاصله لازمه.\n"
            f"📦 حداکثر حجم مجاز هر فایل: {MAX_FILE_SIZE_MB} مگابایت.\n"
            "⚠️ *توجه:* پست‌های اکانت‌های خصوصی پشتیبانی نمی‌شن."
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
        user = query.from_user
        text = (
            f"✨ *سلام {user.first_name or 'دوست عزیز'} 👋*\n\n"
            "🔗 لینک موردنظرت رو برام بفرست.\n\n"
            "👇 یکی از گزینه‌های زیر رو انتخاب کن یا مستقیم لینک بفرست:"
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
# دریافت لینک از کاربر
# ---------------------------------------------------------------------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    if not parsed.scheme.startswith("http") or not any(d in domain for d in SUPPORTED_DOMAINS):
        await update.message.reply_text(
            "🚫 لطفاً یک لینک معتبر از اینستاگرام، یوتیوب، توییتر/X، تیک‌تاک یا فیسبوک بفرست.",
            reply_markup=main_menu_keyboard(),
        )
        return

    user_id = update.effective_user.id
    allowed, wait_left = check_rate_limit(user_id)
    if not allowed:
        await update.message.reply_text(
            f"⏱ خیلی سریع درخواست دادی! لطفاً {wait_left} ثانیه دیگه دوباره امتحان کن."
        )
        return

    short_id = uuid.uuid4().hex[:8]
    url_cache[short_id] = url

    await update.message.reply_text(
        "🎚 کیفیت موردنظرت رو انتخاب کن:\n"
        "(اگه لینک، پست چندعکسی/کاروسل باشه، همه‌ی آیتم‌ها با کیفیت انتخابی برات ارسال می‌شن)",
        reply_markup=quality_keyboard(short_id),
    )


# ---------------------------------------------------------------------------
# افزودن درخواست به صف
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


# ---------------------------------------------------------------------------
# پردازش واقعی دانلود (اجرا شده توسط ورکرهای صف)
# ---------------------------------------------------------------------------
async def safe_edit_status(status_msg, text, parse_mode=None, reply_markup=None):
    try:
        await status_msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        # اگه متن تغییری نکرده یا پیام حذف شده باشه، خطا رو نادیده بگیر
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
    last_reported = {"percent": -1}

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
        if percent - last_reported["percent"] >= 20 or percent >= 99:
            last_reported["percent"] = percent
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

    # هر دانلود پوشه‌ی موقت مخصوص به خودش رو داره تا فایل‌های کاربران مختلف قاطی نشن
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": f"{job_dir}/%(id)s.%(ext)s",
        "format": fmt,
        "quiet": True,
        "noplaylist": False,  # اجازه بده کاروسل‌ها (چند آیتمی) کامل دانلود بشن
        "progress_hooks": [progress_hook],
    }
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
                    f"و از حد مجاز ({MAX_FILE_SIZE_MB} مگابایت) بیشتره، ارسال نشد.\n"
                    "می‌تونی کیفیت پایین‌تری انتخاب کنی."
                )
                continue

            caption_parts = []
            if not sent_any:
                caption_parts.append("✅ دانلود با موفقیت انجام شد!")
                if original_caption:
                    caption_parts.append(f"\n📝 کپشن اصلی:\n{original_caption}")
            if total > 1:
                caption_parts.append(f"\n({idx}/{total})")
            caption_parts.append("\n🙏 با تشکر از امپراطور")
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
        record_download(user_id, username)

    except DownloadCancelled:
        await safe_edit_status(status_msg, "⛔️ دانلود توسط شما لغو شد.")

    except Exception as e:
        logger.exception("خطا در دانلود")
        record_error()
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
# ورکرهای صف
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


# ---------------------------------------------------------------------------
# راه‌اندازی ربات
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
