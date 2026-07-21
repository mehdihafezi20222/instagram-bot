import os
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


async def download_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "instagram.com" not in url:
        await update.message.reply_text(
            "🚫 لطفاً یک لینک معتبر اینستاگرام بفرست.",
            reply_markup=main_menu_keyboard(),
        )
        return

    status_msg = await update.message.reply_text("⏳ در حال دانلود، چند لحظه صبر کن...")

    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "format": "best",
        "quiet": True,
        "noplaylist": True,
    }

    filename = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        caption = "✅ دانلود با موفقیت انجام شد!\n\n🙏 با تشکر از امپراطور"

        if filename.lower().endswith((".mp4", ".mov", ".webm")):
            with open(filename, "rb") as f:
                await update.message.reply_video(video=f, caption=caption)
        else:
            with open(filename, "rb") as f:
                await update.message.reply_photo(photo=f, caption=caption)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در دانلود:\n`{e}`", parse_mode=ParseMode.MARKDOWN)

    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_instagram))
    print("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
