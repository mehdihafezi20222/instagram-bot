import os
import json
import uuid
import shutil
import time
import asyncio
import logging
from datetime import date
from urllib.parse import urlparse
from cachetools import TTLCache

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
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

# محدودیت تعداد دانلود همزمان تا سرور اورلود نشه
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# دانلودهای در حال اجرا: job_id -> asyncio.Task (برای قابلیت لغو دانلود)
active_downloads = {}

# سقف دانلود روزانه برای کاربران عادی (ادمین و VIP نامحدودند)
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "10"))

# پیام تشکر که بعد از هر دانلود موفق فرستاده می‌شود
CREDIT_MESSAGE = "🙏 با تشکر از امپراطور ۲۷"

# چند ثانیه یک پوشه‌ی دانلود یتیم روی دیسک باقی بماند قبل از پاکسازی خودکار
ORPHAN_MAX_AGE_SECONDS = 3600

SUPPORTED_DOMAINS = [
    "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "facebook.com", "fb.watch"
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")

# متن دکمه‌های منو - برای تشخیص از لینک استفاده می‌شود
MENU_TEXTS = {
    "📥 دانلود", "👤 پروفایل", "⚙️ تنظیمات", "🤝 تعامل",
    "🔔 اعلان‌ها", "🚀 امکانات حرفه‌ای", "🛠 پنل ادمین", "💬 پشتیبانی",
}

# ---------------------------------------------------------------------------
# ذخیره‌سازی ساده JSON
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
                data.setdefault("broadcast_count", 0)
                data.setdefault("last_broadcast_failed", [])
                return data
        except Exception:
            pass

    return {
        "total_downloads": 0,
        "total_errors": 0,
        "users": {},
        "banned": [],
        "maintenance": False,
        "broadcast_count": 0,
        "last_broadcast_failed": [],
    }

def save_stats(data):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("خطا در ذخیره stats.json")

stats = load_stats()

DEFAULT_USER = {
    "count": 0,
    "username": "",
    "notify": True,
    "dark": False,
    "language": "fa",
    "last_action": "",
    "history": [],
    "vip": False,
    "daily_count": 0,
    "daily_date": "",
    "joined_notified": False,
}

async def record_user(user_id: int, username: str):
    async with stats_lock:
        uid = str(user_id)
        if uid not in stats["users"]:
            stats["users"][uid] = dict(DEFAULT_USER)
            stats["users"][uid]["username"] = username or ""
        else:
            if username:
                stats["users"][uid]["username"] = username
            for k, v in DEFAULT_USER.items():
                stats["users"][uid].setdefault(k, v)
        save_stats(stats)

async def record_download(user_id: int, url: str = "", quality: str = ""):
    async with stats_lock:
        stats["total_downloads"] += 1
        uid = str(user_id)
        if uid in stats["users"]:
            stats["users"][uid]["count"] += 1
            stats["users"][uid]["last_action"] = "download"
            if url:
                history_item = {
                    "url": url,
                    "quality": quality,
                }
                stats["users"][uid]["history"].insert(0, history_item)
                stats["users"][uid]["history"] = stats["users"][uid]["history"][:10]
        save_stats(stats)

async def record_error():
    async with stats_lock:
        stats["total_errors"] += 1
        save_stats(stats)

# ---------------------------------------------------------------------------
# منوها
# ---------------------------------------------------------------------------
def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["📥 دانلود", "👤 پروفایل"],
            ["⚙️ تنظیمات", "🤝 تعامل"],
            ["🔔 اعلان‌ها", "🚀 امکانات حرفه‌ای"],
            ["🛠 پنل ادمین", "💬 پشتیبانی"],
        ],
        resize_keyboard=True
    )

def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 تغییر زبان", callback_data="settings_lang"),
            InlineKeyboardButton("🌙 حالت شب", callback_data="settings_dark"),
        ],
        [
            InlineKeyboardButton("🔔 اعلان‌ها", callback_data="settings_notify"),
            InlineKeyboardButton("♻️ بازنشانی تنظیمات", callback_data="settings_reset"),
        ],
        [
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"),
        ]
    ])

def interaction_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ امتیاز دادن", callback_data="rate"),
            InlineKeyboardButton("💬 ارسال بازخورد", callback_data="feedback"),
        ],
        [
            InlineKeyboardButton("📢 دعوت دوستان", callback_data="invite"),
            InlineKeyboardButton("📨 ارسال پیام", callback_data="sendmsg"),
        ],
        [
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"),
        ]
    ])

def pro_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار من", callback_data="my_stats"),
            InlineKeyboardButton("🕘 تاریخچه دانلود", callback_data="history"),
        ],
        [
            InlineKeyboardButton("⭐ VIP", callback_data="vip"),
            InlineKeyboardButton("🎁 کد دعوت", callback_data="referral"),
        ],
        [
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"),
        ]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار ربات", callback_data="admin_stats"),
            InlineKeyboardButton("👥 کاربران", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("📢 ارسال همگانی", callback_data="admin_broadcast"),
            InlineKeyboardButton("🚫 مدیریت کاربران", callback_data="admin_users_manage"),
        ],
        [
            InlineKeyboardButton("🔧 تنظیمات ربات", callback_data="admin_settings"),
            InlineKeyboardButton("🛠 تغییر حالت تعمیرات", callback_data="admin_maintenance"),
        ],
        [
            InlineKeyboardButton("❌ بستن پنل", callback_data="admin_close"),
        ]
    ])

def quality_keyboard(short_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 بهترین کیفیت (ویدیو)", callback_data=f"q:best:{short_id}")],
        [InlineKeyboardButton("📺 کیفیت متوسط (720p)", callback_data=f"q:720:{short_id}")],
        [InlineKeyboardButton("🎧 فقط صدا (MP3)", callback_data=f"q:audio:{short_id}")],
        [InlineKeyboardButton("❌ انصراف", callback_data="cancel_select")]
    ])

def cancel_download_keyboard(job_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو دانلود", callback_data=f"cancel_dl:{job_id}")]
    ])

def redo_keyboard(short_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 دانلود دوباره با کیفیت دیگه", callback_data=f"redo:{short_id}")]
    ])

def force_join_keyboard():
    ch_clean = CHANNEL_USERNAME.replace("@", "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{ch_clean}")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join")]
    ])

# ---------------------------------------------------------------------------
# ابزارهای کمکی
# ---------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_user_record(user_id: int):
    uid = str(user_id)
    rec = stats["users"].setdefault(uid, dict(DEFAULT_USER))
    for k, v in DEFAULT_USER.items():
        rec.setdefault(k, v)
    return rec

def human_size(num_bytes):
    try:
        num = float(num_bytes)
    except (TypeError, ValueError):
        return "نامشخص"
    for unit in ["B", "KB", "MB", "GB"]:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"

def queue_waiting_count():
    waiters = getattr(download_semaphore, "_waiters", None)
    return len(waiters) if waiters else 0

def check_daily_limit(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    u = get_user_record(user_id)
    if u.get("vip"):
        return True
    today = date.today().isoformat()
    if u.get("daily_date") != today:
        return True
    return u.get("daily_count", 0) < DAILY_LIMIT

def increment_daily_count(user_id: int):
    u = get_user_record(user_id)
    today = date.today().isoformat()
    if u.get("daily_date") != today:
        u["daily_date"] = today
        u["daily_count"] = 0
    u["daily_count"] = u.get("daily_count", 0) + 1
    save_stats(stats)

async def is_channel_member(bot, user_id: int) -> bool:
    if not CHANNEL_USERNAME or is_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]
    except Exception:
        # توجه: اگر چک عضویت با خطا مواجه شود (مثلاً ربات ادمین کانال نیست)
        # به صورت پیش‌فرض اجازه عبور داده می‌شود.
        logger.warning("خطا در بررسی عضویت کاربر %s در کانال", user_id)
        return True

async def fetch_preview_info(url: str):
    """اطلاعات سبک لینک (بدون دانلود) برای نمایش عنوان و حجم تقریبی."""
    loop = asyncio.get_running_loop()

    def _probe():
        opts = {
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'skip_download': True,
            'noplaylist': True,
            'user_agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await loop.run_in_executor(None, _probe)
    except Exception:
        return None

def estimate_size(info: dict):
    if not info:
        return None
    size = info.get("filesize") or info.get("filesize_approx")
    if size:
        return size
    formats = info.get("formats") or []
    sizes = [
        f.get("filesize") or f.get("filesize_approx")
        for f in formats
        if f.get("filesize") or f.get("filesize_approx")
    ]
    return max(sizes) if sizes else None

# ---------------------------------------------------------------------------
# پیام‌های عمومی
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in stats.get("banned", []):
        await update.message.reply_text("🚫 شما مسدود شده‌اید.")
        return

    await update.message.reply_text(
        f"سلام {user.first_name or 'دوست عزیز'} 👋\n\n"
        "لینک پست یا ویدیو را بفرست تا گزینه‌های دانلود را نشان بدهم.",
        reply_markup=main_menu()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنما:\n"
        "• لینک اینستاگرام، یوتیوب، تیک‌تاک، توییتر یا فیسبوک بفرست\n"
        "• از منو برای تنظیمات و امکانات بیشتر استفاده کن\n"
        "• ادمین‌ها می‌توانند از پنل ادمین استفاده کنند",
        reply_markup=main_menu()
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user_record(user.id)
    await update.message.reply_text(
        f"📊 آمار شما\n\n"
        f"👤 نام: {user.first_name or '-'}\n"
        f"📥 دانلودها: {u.get('count', 0)}\n"
        f"📆 دانلود امروز: {u.get('daily_count', 0)}/{'نامحدود' if (u.get('vip') or is_admin(user.id)) else DAILY_LIMIT}\n"
        f"🕘 آخرین فعالیت: {u.get('last_action', 'ندارد')}\n"
        f"🔔 اعلان‌ها: {'فعال' if u.get('notify', True) else 'خاموش'}\n"
        f"🌙 حالت شب: {'فعال' if u.get('dark', False) else 'خاموش'}"
    )

# ---------------------------------------------------------------------------
# پنل ادمین
# ---------------------------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔ دسترسی غیرمجاز!\nآیدی تلگرام شما: `{user_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    text = (
        "🛠 *پنل مدیریت ربات*\n\n"
        f"👥 تعداد کاربران: {len(stats['users'])}\n"
        f"📥 دانلودهای موفق: {stats['total_downloads']}\n"
        f"❌ مجموع خطاها: {stats['total_errors']}\n"
        f"🛠 حالت تعمیرات: {'فعال 🛠' if stats.get('maintenance') else 'غیرفعال 🟢'}\n"
        f"🚫 کاربران مسدود: {len(stats.get('banned', []))}\n"
        f"📢 ارسال همگانی انجام‌شده: {stats.get('broadcast_count', 0)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔ دسترسی غیرمجاز!\nآیدی تلگرام شما: `{user_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    msg_to_send = update.message.reply_to_message
    text = " ".join(context.args) if context.args else ""

    if not msg_to_send and not text:
        await update.message.reply_text(
            "⚠️ نحوه استفاده از دستور همگانی:\n\n"
            "1) پیام متنی:\n"
            "`/broadcast سلام به همه`\n\n"
            "2) عکس/ویدیو:\n"
            "روی پیام ریپلای کن و بزن `/broadcast`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    status_msg = await update.message.reply_text("🚀 در حال ارسال پیام همگانی...")
    success, failed = 0, 0
    failed_ids = []

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
            failed_ids.append(uid_str)

    async with stats_lock:
        stats["broadcast_count"] = stats.get("broadcast_count", 0) + 1
        stats["last_broadcast_failed"] = failed_ids
        save_stats(stats)

    report = f"📊 گزارش ارسال همگانی:\n\n✅ موفق: {success}\n❌ ناموفق: {failed}"
    if failed_ids:
        shown = ", ".join(failed_ids[:20])
        more = "" if len(failed_ids) <= 20 else f" (+{len(failed_ids) - 20} مورد دیگر)"
        report += f"\n\n🚫 آیدی‌های ناموفق:\n{shown}{more}"

    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

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

async def set_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        u = get_user_record(uid)
        u["vip"] = not u.get("vip", False)
        save_stats(stats)
        await update.message.reply_text(
            f"⭐ وضعیت VIP کاربر {uid}: {'فعال ✅' if u['vip'] else 'غیرفعال ❌'}"
        )
    else:
        await update.message.reply_text("استفاده: `/setvip USER_ID`", parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# متن‌های منو
# ---------------------------------------------------------------------------
async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    await record_user(user.id, user.username)

    if text == "📥 دانلود":
        await update.message.reply_text(
            "لینک پست یا ویدیو را بفرست تا گزینه‌های دانلود را نشان بدهم."
        )

    elif text == "👤 پروفایل":
        u = get_user_record(user.id)
        await update.message.reply_text(
            f"👤 پروفایل شما\n\n"
            f"نام: {user.first_name or '-'}\n"
            f"یوزرنیم: @{user.username if user.username else 'ندارد'}\n"
            f"آیدی: {user.id}\n"
            f"تعداد دانلود: {u.get('count', 0)}\n"
            f"اعلان‌ها: {'فعال' if u.get('notify', True) else 'خاموش'}\n"
            f"حالت شب: {'فعال' if u.get('dark', False) else 'خاموش'}\n"
            f"زبان: {u.get('language', 'fa')}\n"
            f"VIP: {'فعال ⭐' if u.get('vip') else 'غیرفعال'}"
        )

    elif text == "⚙️ تنظیمات":
        await update.message.reply_text("⚙️ تنظیمات:", reply_markup=settings_keyboard())

    elif text == "🤝 تعامل":
        await update.message.reply_text("🤝 بخش تعامل:", reply_markup=interaction_keyboard())

    elif text == "🔔 اعلان‌ها":
        u = get_user_record(user.id)
        status = "فعال ✅" if u.get("notify", True) else "خاموش ❌"
        await update.message.reply_text(
            f"🔔 اعلان‌ها\n\nوضعیت فعلی: {status}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ فعال کردن", callback_data="notify_on"),
                    InlineKeyboardButton("❌ خاموش کردن", callback_data="notify_off"),
                ],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")]
            ])
        )

    elif text == "🚀 امکانات حرفه‌ای":
        await update.message.reply_text("🚀 امکانات حرفه‌ای:", reply_markup=pro_keyboard())

    elif text == "🛠 پنل ادمین":
        await admin_panel(update, context)

    elif text == "💬 پشتیبانی":
        await update.message.reply_text(
            "💬 برای پشتیبانی، پیام خود را همینجا بفرست.\n"
            "اگر خواستی بعداً می‌شود این بخش را به ادمین وصل کرد."
        )

    else:
        # اگر متن هیچکدام از دکمه‌های منو نبود، به‌عنوان لینک بررسی می‌شود
        await handle_link(update, context)

# ---------------------------------------------------------------------------
# دریافت و پردازش لینک (متن مستقیم یا کپشن پیام فوروارد‌شده)
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
        await update.message.reply_text(
            "⚠️ ابتدا در کانال ما عضو شوید:",
            reply_markup=force_join_keyboard()
        )
        return

    raw_text = (update.message.text or update.message.caption or "").strip()
    if not raw_text:
        return

    url = raw_text
    if "?" in url and ("tiktok.com" in url or "instagram.com" in url):
        url = url.split("?")[0]

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    if not parsed.scheme.startswith("http") or not any(d in domain for d in SUPPORTED_DOMAINS):
        # اگر پیام صرفاً یک کپشن معمولی بدون لینک شناخته‌شده باشد، سکوت می‌کنیم
        if update.message.text:
            await update.message.reply_text(
                "⚠️ لینک معتبر نیست یا از این سایت‌ها پشتیبانی نمی‌شود.\n"
                "سایت‌های پشتیبانی‌شده: اینستاگرام، یوتیوب، تیک‌تاک، توییتر/X، فیسبوک"
            )
        return

    if not check_daily_limit(user.id):
        await update.message.reply_text(
            f"⛔ شما به سقف دانلود روزانه ({DAILY_LIMIT} مورد) رسیدید.\n"
            "فردا دوباره امتحان کنید یا برای دسترسی نامحدود VIP بگیرید."
        )
        return

    short_id = uuid.uuid4().hex[:8]
    url_cache[short_id] = url

    status_msg = await update.message.reply_text("🔎 در حال بررسی لینک...")

    preview_info = await fetch_preview_info(url)
    title_line = ""
    size_line = ""
    if preview_info:
        title = (preview_info.get("title") or "").strip()
        if title:
            title_line = f"🎬 {title[:150]}\n"
        size_bytes = estimate_size(preview_info)
        if size_bytes:
            size_line = f"📦 حجم تقریبی: {human_size(size_bytes)}\n"

    try:
        await status_msg.edit_text(
            f"{title_line}{size_line}\n🎚 کیفیت مورد نظرت را انتخاب کن:",
            reply_markup=quality_keyboard(short_id)
        )
    except Exception:
        await status_msg.edit_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id))

# ---------------------------------------------------------------------------
# هندلر دکمه‌ها
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # -----------------------------
    # بازگشت به منوی اصلی
    # -----------------------------
    if data == "back_main":
        try:
            await query.message.edit_text("🏠 منوی اصلی", reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("یکی از گزینه‌ها را انتخاب کن:", reply_markup=main_menu())
        return

    # -----------------------------
    # بررسی مجدد عضویت کانال
    # -----------------------------
    if data == "check_join":
        if await is_channel_member(context.bot, user.id):
            u = get_user_record(user.id)
            if not u.get("joined_notified"):
                u["joined_notified"] = True
                save_stats(stats)
                await query.message.edit_text(
                    "✅ عضویت شما تایید شد. ممنون که به ما پیوستی 🙌\n"
                    "حالا می‌تونی لینک بفرستی."
                )
            else:
                await query.message.edit_text("✅ عضویت شما تایید شد. حالا می‌تونی لینک بفرستی.")
        else:
            await query.answer("⚠️ هنوز عضو کانال نشده‌اید.", show_alert=True)
        return

    # -----------------------------
    # تنظیمات
    # -----------------------------
    if data.startswith("settings_"):
        u = get_user_record(user.id)

        if data == "settings_lang":
            current = u.get("language", "fa")
            new_lang = "en" if current == "fa" else "fa"
            u["language"] = new_lang
            save_stats(stats)
            await query.message.edit_text(f"🌐 زبان تغییر کرد: {new_lang}")
            return

        if data == "settings_dark":
            u["dark"] = not u.get("dark", False)
            save_stats(stats)
            await query.message.edit_text(
                f"🌙 حالت شب: {'فعال ✅' if u['dark'] else 'خاموش ❌'}"
            )
            return

        if data == "settings_notify":
            u["notify"] = not u.get("notify", True)
            save_stats(stats)
            await query.message.edit_text(
                f"🔔 اعلان‌ها: {'فعال ✅' if u['notify'] else 'خاموش ❌'}"
            )
            return

        if data == "settings_reset":
            u["notify"] = True
            u["dark"] = False
            u["language"] = "fa"
            save_stats(stats)
            await query.message.edit_text("♻️ تنظیمات به حالت پیش‌فرض برگشت.")
            return

    # -----------------------------
    # تعامل
    # -----------------------------
    if data == "rate":
        await query.message.edit_text(
            "⭐ از ربات راضی هستی؟\n\n"
            "فعلاً بخش امتیازدهی نمایشی است و بعداً می‌شود به دیتابیس وصلش کرد."
        )
        return

    if data == "feedback":
        await query.message.edit_text(
            "💬 بازخوردت را همینجا بفرست.\n"
            "بعداً می‌شود این بخش را به فرم یا ذخیره‌سازی وصل کرد."
        )
        return

    if data == "invite":
        await query.message.edit_text(
            f"📢 لینک دعوت شما:\n\n"
            f"`https://t.me/{context.bot.username}?start={user.id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data == "sendmsg":
        await query.message.edit_text(
            "📨 این بخش فعلاً نمایشی است.\n"
            "بعداً می‌شود آن را به سیستم پیام‌دهی داخلی وصل کرد."
        )
        return

    # -----------------------------
    # اعلان‌ها
    # -----------------------------
    if data == "notify_on":
        u = get_user_record(user.id)
        u["notify"] = True
        save_stats(stats)
        await query.message.edit_text("🔔 اعلان‌ها فعال شد ✅")
        return

    if data == "notify_off":
        u = get_user_record(user.id)
        u["notify"] = False
        save_stats(stats)
        await query.message.edit_text("🔕 اعلان‌ها خاموش شد ❌")
        return

    # -----------------------------
    # امکانات حرفه‌ای
    # -----------------------------
    if data == "my_stats":
        u = get_user_record(user.id)
        limit_text = "نامحدود" if (u.get("vip") or is_admin(user.id)) else str(DAILY_LIMIT)
        await query.message.edit_text(
            f"📊 آمار شما\n\n"
            f"👤 نام: {user.first_name or '-'}\n"
            f"📥 دانلودها: {u.get('count', 0)}\n"
            f"📆 دانلود امروز: {u.get('daily_count', 0)}/{limit_text}\n"
            f"🕘 آخرین فعالیت: {u.get('last_action', 'ندارد')}\n"
            f"🔔 اعلان‌ها: {'فعال' if u.get('notify', True) else 'خاموش'}\n"
            f"🌙 حالت شب: {'فعال' if u.get('dark', False) else 'خاموش'}"
        )
        return

    if data == "history":
        u = get_user_record(user.id)
        history = u.get("history", [])
        if not history:
            await query.message.edit_text("🕘 هنوز تاریخچه‌ای ثبت نشده است.")
            return

        lines = ["🕘 تاریخچه دانلود شما:\n"]
        for i, item in enumerate(history[:10], start=1):
            q = item.get("quality", "-")
            url = item.get("url", "-")
            lines.append(f"{i}. کیفیت: {q}\n{url}\n")
        await query.message.edit_text("\n".join(lines))
        return

    if data == "vip":
        u = get_user_record(user.id)
        status = "فعال ✅" if u.get("vip") else "غیرفعال ❌"
        await query.message.edit_text(
            f"⭐ بخش VIP\n\n"
            f"وضعیت فعلی شما: {status}\n\n"
            "کاربران VIP از سقف دانلود روزانه معاف هستند.\n"
            "برای فعال‌سازی با پشتیبانی در ارتباط باشید."
        )
        return

    if data == "referral":
        await query.message.edit_text(
            f"🎁 کد دعوت شما:\n\n`{user.id}`\n\n"
            f"لینک دعوت:\n`https://t.me/{context.bot.username}?start={user.id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # -----------------------------
    # پنل ادمین
    # -----------------------------
    if data.startswith("admin_"):
        if user.id not in ADMIN_IDS:
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return

        if data == "admin_stats":
            await query.message.edit_text(
                "📊 آمار ربات\n\n"
                f"👥 کاربران: {len(stats['users'])}\n"
                f"📥 دانلود موفق: {stats['total_downloads']}\n"
                f"❌ خطاها: {stats['total_errors']}\n"
                f"🚫 بن شده: {len(stats['banned'])}\n"
                f"🛠 حالت تعمیرات: {'فعال' if stats.get('maintenance') else 'غیرفعال'}\n"
                f"📢 ارسال همگانی: {stats.get('broadcast_count', 0)}",
                reply_markup=admin_keyboard()
            )
            return

        if data == "admin_users":
            await query.message.edit_text(
                f"👥 تعداد کاربران ثبت‌شده: {len(stats['users'])}",
                reply_markup=admin_keyboard()
            )
            return

        if data == "admin_users_manage":
            await query.message.edit_text(
                "🚫 مدیریت کاربران\n\n"
                "دستورها:\n"
                "/ban USER_ID\n"
                "/unban USER_ID\n"
                "/setvip USER_ID",
                reply_markup=admin_keyboard()
            )
            return

        if data == "admin_settings":
            await query.message.edit_text(
                "🔧 تنظیمات ربات\n\n"
                "فعلاً تنظیمات اصلی همین‌ها هستند:\n"
                "• کانال اجباری\n"
                "• حالت تعمیرات\n"
                "• مدیریت کاربران\n"
                "• ارسال همگانی\n"
                f"• سقف دانلود روزانه: {DAILY_LIMIT}",
                reply_markup=admin_keyboard()
            )
            return

        if data == "admin_maintenance":
            stats["maintenance"] = not stats.get("maintenance", False)
            save_stats(stats)
            status = "فعال 🛠" if stats["maintenance"] else "غیرفعال 🟢"
            await query.message.edit_text(
                f"حالت تعمیرات: {status}",
                reply_markup=admin_keyboard()
            )
            return

        if data == "admin_broadcast":
            await query.message.edit_text(
                "📢 ارسال همگانی\n\n"
                "از دستور زیر استفاده کن:\n\n"
                "`/broadcast متن پیام`\n\n"
                "یا روی یک پیام ریپلای کن و `/broadcast` بزن.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard()
            )
            return

        if data == "admin_close":
            await query.message.delete()
            return

    # -----------------------------
    # کیفیت دانلود
    # -----------------------------
    if data == "cancel_select":
        await query.message.delete()
        return

    if data.startswith("redo:"):
        short_id = data.split(":", 1)[1]
        url = url_cache.get(short_id)
        if not url:
            await query.message.edit_text("⚠️ لینک منقضی شده، لینک رو دوباره بفرست.")
            return
        await query.message.edit_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id))
        return

    if data.startswith("cancel_dl:"):
        job_id = data.split(":", 1)[1]
        task = active_downloads.get(job_id)
        if task and not task.done():
            task.cancel()
        else:
            await query.answer("این دانلود قبلاً به پایان رسیده.", show_alert=True)
        return

    if data.startswith("q:"):
        _, quality, short_id = data.split(":", 2)
        url = url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، دوباره بفرستید.")
            return

        if not check_daily_limit(user.id):
            await query.message.reply_text(
                f"⛔ شما به سقف دانلود روزانه ({DAILY_LIMIT} مورد) رسیدید."
            )
            return

        await query.message.delete()
        job_id = uuid.uuid4().hex[:8]
        status_msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="⏳ در حال دانلود... لطفاً شکیبا باشید.",
            reply_markup=cancel_download_keyboard(job_id)
        )
        task = asyncio.create_task(
            download_and_send(status_msg, user, url, quality, job_id, short_id)
        )
        active_downloads[job_id] = task
        return

# ---------------------------------------------------------------------------
# موتور اصلی دانلود
# ---------------------------------------------------------------------------
async def download_and_send(status_msg, user, url, quality, job_id, short_id):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ydl_opts = {
        'outtmpl': f'{job_dir}/%(id)s_%(autonumber)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'extractor_args': {
            'youtube': {'player_client': ['android', 'web']}
        },
    }

    if quality == "audio":
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif quality == "720":
        ydl_opts['format'] = 'best[height<=720]/best'

    loop = asyncio.get_running_loop()

    if download_semaphore.locked():
        pos = queue_waiting_count() + 1
        try:
            await status_msg.edit_text(
                f"⏳ در صف دانلود هستید... ({pos} نفر جلوتر از شما)",
                reply_markup=cancel_download_keyboard(job_id)
            )
        except Exception:
            pass

    async with download_semaphore:
        try:
            try:
                await status_msg.edit_text(
                    "⏳ در حال دانلود... لطفاً شکیبا باشید.",
                    reply_markup=cancel_download_keyboard(job_id)
                )
            except Exception:
                pass

            def _extract():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=True)

            info = await loop.run_in_executor(None, _extract)

            if not info:
                raise RuntimeError("اطلاعاتی دریافت نشد. ممکنه لینک خصوصی یا نامعتبر باشه.")

            files = [
                os.path.join(job_dir, f)
                for f in os.listdir(job_dir)
                if os.path.isfile(os.path.join(job_dir, f))
            ]

            if not files:
                raise RuntimeError("فایلی دانلود نشد.")

            try:
                await status_msg.edit_text("📤 در حال ارسال فایل...", reply_markup=None)
            except Exception:
                pass

            for fname in files:
                lower = fname.lower()
                with open(fname, "rb") as f:
                    if lower.endswith(IMAGE_EXTS):
                        await status_msg.reply_photo(photo=f, caption="✅ دانلود شد!")
                    elif lower.endswith(AUDIO_EXTS):
                        await status_msg.reply_audio(audio=f, caption="✅ فایل صوتی دانلود شد!")
                    else:
                        await status_msg.reply_video(video=f, caption="✅ ویدیو دانلود شد!")

            # ارسال کپشن پست (اینستاگرام/یوتیوب/تیک‌تاک/...) به‌عنوان پیام جدا
            caption_text = (info.get("description") or info.get("title") or "").strip()
            if caption_text:
                await status_msg.reply_text(f"📝 کپشن:\n\n{caption_text[:1000]}")

            # پیام تشکر جداگانه
            await status_msg.reply_text(CREDIT_MESSAGE)

            # دکمه دانلود دوباره با کیفیت دیگر
            await status_msg.reply_text(
                "دوست داری با کیفیت دیگه‌ای هم دانلود کنی؟",
                reply_markup=redo_keyboard(short_id)
            )

            await status_msg.delete()
            await record_download(user.id, url=url, quality=quality)
            increment_daily_count(user.id)

        except asyncio.CancelledError:
            try:
                await status_msg.edit_text("❌ دانلود لغو شد.", reply_markup=None)
            except Exception:
                pass
            raise

        except Exception as e:
            logger.exception("خطا در دانلود")
            await record_error()
            err_text = str(e)[:300]
            try:
                await status_msg.edit_text(f"❌ خطا در دانلود:\n`{err_text}`", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception:
                await status_msg.reply_text(f"❌ خطا در دانلود:\n{err_text}")

        finally:
            active_downloads.pop(job_id, None)
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
# پاکسازی خودکار پوشه‌های یتیم (اجرا به‌صورت دوره‌ای)
# ---------------------------------------------------------------------------
async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, name)
            if not os.path.isdir(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                continue
            if now - mtime > ORPHAN_MAX_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                logger.info("پوشه یتیم پاکسازی شد: %s", path)
    except Exception:
        logger.exception("خطا در پاکسازی خودکار")

# ---------------------------------------------------------------------------
# اجرای اصلی
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("setvip", set_vip))
    app.add_handler(CommandHandler("off", toggle_maintenance))
    app.add_handler(CommandHandler("on", toggle_maintenance))

    app.add_handler(CallbackQueryHandler(button_handler))

    # یک هندلر برای متن: هم منو، هم لینک مستقیم را مدیریت می‌کند
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            menu_text_handler
        )
    )

    # پیام‌های فوروارد/عکس/ویدیوی دارای کپشن که ممکن است لینک باشند
    app.add_handler(
        MessageHandler(
            filters.CAPTION & ~filters.COMMAND,
            handle_link
        )
    )

    if app.job_queue is not None:
        app.job_queue.run_repeating(cleanup_job, interval=1800, first=60)
    else:
        logger.warning(
            "JobQueue فعال نیست؛ برای پاکسازی خودکار پکیج "
            "'python-telegram-bot[job-queue]' را نصب کنید."
        )

    print("ربات روشن شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
