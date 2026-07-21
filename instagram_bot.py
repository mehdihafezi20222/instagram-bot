import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import yt_dlp

logging.basicConfig(level=logging.INFO)

# توکن ربات از متغیر محیطی BOT_TOKEN خونده می‌شه (روی Render/Railway تنظیمش می‌کنی)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 راهنما", callback_data="help")],
        [InlineKeyboardButton("ℹ️ درباره ربات", callback_data="about")],
    ])
    await update.message.reply_text(
        "سلام! لینک پست، ریلز یا استوری اینستاگرام رو برام بفرست تا دانلودش کنم.",
        reply_markup=keyboard,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "help":
        await query.edit_message_text(
            "📖 راهنما:\n\n"
            "۱. لینک پست/ریلز/استوری اینستاگرام رو کپی کن\n"
            "۲. بفرستش برای من\n"
            "۳. فایل رو برات دانلود و ارسال می‌کنم\n\n"
            "توجه: پست‌های اکانت‌های خصوصی پشتیبانی نمی‌شن."
        )
    elif query.data == "about":
        await query.edit_message_text(
            "ℹ️ این ربات با پایتون و کتابخانه yt-dlp ساخته شده و برای دانلود محتوای عمومی اینستاگرام استفاده می‌شه."
        )


async def download_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "instagram.com" not in url:
        await update.message.reply_text("لطفاً یک لینک معتبر اینستاگرام بفرست.")
        return

    status_msg = await update.message.reply_text("در حال دانلود... ⏳")

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

        if filename.lower().endswith((".mp4", ".mov", ".webm")):
            with open(filename, "rb") as f:
                await update.message.reply_video(video=f)
        else:
            with open(filename, "rb") as f:
                await update.message.reply_photo(photo=f)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"خطا در دانلود:\n{e}")

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
