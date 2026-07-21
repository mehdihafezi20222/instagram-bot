import os
import time
import uuid
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
import yt_dlp

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# تنظیمات محدودیت نرخ درخواست (Rate Limit)
# ---------------------------------------------------------------------------
RATE_LIMIT_SECONDS = 20  # فاصله زمانی مجاز بین دو درخواست هر کاربر
last_request_time = {}  # user_id -> timestamp آخرین درخواست

# ---------------------------------------------------------------------------
# کش لینک‌ها برای دکمه «دانلود مجدد» (چون callback_data محدودیت طول داره)
# ---------------------------------------------------------------------------
url_cache = {}  # short_id -> url

# حداکثر طول کپشن اصلی پست که ارسال می‌شه
MAX_CAPTION_LEN = 500


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 راهنما", callback_data="help"),
         InlineKeyboardButton("ℹ️ درباره ربات", callback_data="about")],
        [InlineKeyboardButton("💬 پشتیبانی", url="https://t.me/")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back")]
    ])


def redownload_keyboard(short_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 دانلود مجدد", callback_data=f"redl:{short_id}")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✨ *به ربات دانلودر اینستاگرام خوش اومدی!*\n\n"
        "🔗 فقط کافیه لینک پست، ریلز یا استوری اینستاگرام رو برام بفرستی؛ "
        "بقیه‌ش با من 😉\n\n"
        "👇 برای شروع یکی از گزینه‌های زیر رو انتخاب کن یا مستقیم لینک بفرست:"
    )
    await update.message.reply_text(
        text,
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "help":
        text = (
            "📖 *راهنمای استفاده*\n\n"
            "۱️⃣ لینک پست، ریلز یا استوری اینستاگرام رو کپی کن\n"
            "۲️⃣ همینجا برام بفرستش\n"
            "۳️⃣ فایل رو براش دانلود و ارسال می‌کنم 🎬\n\n"
            f"⏱ بین هر درخواست حداقل {RATE_LIMIT_SECONDS} ثانیه فاصله لازمه.\n"
            "⚠️ *توجه:* پست‌های اکانت‌های خصوصی پشتیبانی نمی‌شن."
        )
        await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode=ParseMode.MARKDOWN)

    elif query.data == "about":
        text = (
            "ℹ️ *درباره ربات*\n\n"
            "🐍 ساخته‌شده با Python و کتابخانه‌ی yt-dlp\n"
            "📥 مخصوص دانلود محتوای عمومی اینستاگرام\n"
            "🚀 سریع، ساده و رایگان"
        )
        await query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode=ParseMode.MARKDOWN)

    elif query.data == "back":
        text = (
            "✨ *به ربات دانلودر اینستاگرام خوش اومدی!*\n\n"
            "🔗 فقط کافیه لینک پست، ریلز یا استوری اینستاگرام رو برام بفرستی.\n\n"
            "👇 یکی از گزینه‌های زیر رو انتخاب کن یا مستقیم لینک بفرست:"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)

    elif query.data.startswith("redl:"):
        short_id = query.data.split(":", 1)[1]
        url = url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، لطفاً دوباره لینک رو بفرست.")
            return

        user_id = query.from_user.id
        allowed, wait_left = check_rate_limit(user_id)
        if not allowed:
            await query.message.reply_text(
                f"⏱ لطفاً {wait_left} ثانیه دیگه دوباره امتحان کن."
            )
            return

        await process_download(update, context, url, chat_message=query.message)


def check_rate_limit(user_id: int):
    """برمی‌گردونه (مجاز است؟, ثانیه باقیمانده)"""
    now = time.time()
    last = last_request_time.get(user_id, 0)
    elapsed = now - last
    if elapsed < RATE_LIMIT_SECONDS:
        return False, int(RATE_LIMIT_SECONDS - elapsed)
    last_request_time[user_id] = now
    return True, 0


async def download_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "instagram.com" not in url:
        await update.message.reply_text(
            "🚫 لطفاً یک لینک معتبر اینستاگرام بفرست.",
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

    await process_download(update, context, url, chat_message=update.message)


async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, chat_message):
    status_msg = await chat_message.reply_text("⏳ در حال دانلود... ۰٪")

    loop = asyncio.get_event_loop()
    last_reported = {"percent": -1}

    def progress_hook(d):
        if d.get("status") != "downloading":
            return
        try:
            percent_str = d.get("_percent_str", "0%").strip().replace("%", "")
            percent = int(float(percent_str))
        except (ValueError, TypeError):
            return

        # فقط هر ۲۰٪ یک بار پیام آپدیت بشه تا به لیمیت تلگرام نخوریم
        if percent - last_reported["percent"] >= 20 or percent >= 99:
            last_reported["percent"] = percent
            asyncio.run_coroutine_threadsafe(
                safe_edit_status(status_msg, f"⏳ در حال دانلود... {percent}٪"),
                loop,
            )

    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "format": "best",
        "quiet": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
    }

    filename = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, True))
            filename = ydl.prepare_filename(info)

        await safe_edit_status(status_msg, "📤 در حال ارسال فایل...")

        # کپشن اصلی پست اینستاگرام (در صورت وجود)
        original_caption = (info.get("description") or "").strip()
        if len(original_caption) > MAX_CAPTION_LEN:
            original_caption = original_caption[:MAX_CAPTION_LEN].rstrip() + "…"

        caption_parts = ["✅ دانلود با موفقیت انجام شد!"]
        if original_caption:
            caption_parts.append(f"\n📝 کپشن اصلی:\n{original_caption}")
        caption_parts.append("\n🙏 با تشکر از امپراطور")
        caption = "\n".join(caption_parts)

        # ذخیره لینک برای دکمه «دانلود مجدد»
        short_id = uuid.uuid4().hex[:8]
        url_cache[short_id] = url
        markup = redownload_keyboard(short_id)

        if filename.lower().endswith((".mp4", ".mov", ".webm")):
            with open(filename, "rb") as f:
                await chat_message.reply_video(video=f, caption=caption, reply_markup=markup)
        else:
            with open(filename, "rb") as f:
                await chat_message.reply_photo(photo=f, caption=caption, reply_markup=markup)

        await status_msg.delete()

    except Exception as e:
        await safe_edit_status(status_msg, f"❌ خطا در دانلود:\n`{e}`", parse_mode=ParseMode.MARKDOWN)

    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)


async def safe_edit_status(status_msg, text, parse_mode=None):
    try:
        await status_msg.edit_text(text, parse_mode=parse_mode)
    except Exception:
        # اگه متن تغییری نکرده باشه یا پیام حذف شده باشه، خطا رو نادیده بگیر
        pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_instagram))
    print("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
