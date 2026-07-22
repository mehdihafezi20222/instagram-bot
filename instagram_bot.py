import os
import json
import uuid
import asyncio
import logging
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

SUPPORTED_DOMAINS = [
    "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "facebook.com", "fb.watch"
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")

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
    }

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
            stats["users"][uid] = {
                "count": 0,
                "username": username or "",
                "notify": True,
                "dark": False,
                "language": "fa",
                "last_action": "",
                "history": [],
            }
        else:
            if username:
                stats["users"][uid]["username"] = username
            stats["users"][uid].setdefault("notify", True)
            stats["users"][uid].setdefault("dark", False)
            stats["users"][uid].setdefault("language", "fa")
            stats["users"][uid].setdefault("last_action", "")
            stats["users"][uid].setdefault("history", [])
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
    return stats["users"].setdefault(
        uid,
        {
            "count": 0,
            "username": "",
            "notify": True,
            "dark": False,
            "language": "fa",
            "last_action": "",
            "history": [],
        },
    )

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
        return True

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

    async with stats_lock:
        stats["broadcast_count"] = stats.get("broadcast_count", 0) + 1
        save_stats(stats)

    await status_msg.edit_text(
        f"📊 گزارش ارسال همگانی:\n\n✅ موفق: {success}\n❌ ناموفق: {failed}",
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
            f"زبان: {u.get('language', 'fa')}"
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
        await update.message.reply_text(
            "⚠️ ابتدا در کانال ما عضو شوید:",
            reply_markup=force_join_keyboard()
        )
        return

    url = update.message.text.strip()
    if "?" in url and ("tiktok.com" in url or "instagram.com" in url):
        url = url.split("?")[0]

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    if not parsed.scheme.startswith("http") or not any(d in domain for d in SUPPORTED_DOMAINS):
        return

    short_id = uuid.uuid4().hex[:8]
    url_cache[short_id] = url
    await update.message.reply_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id))

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
        await query.message.edit_text(
            f"📊 آمار شما\n\n"
            f"👤 نام: {user.first_name or '-'}\n"
            f"📥 دانلودها: {u.get('count', 0)}\n"
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
        await query.message.edit_text(
            "⭐ بخش VIP\n\n"
            "این بخش فعلاً آماده نیست.\n"
            "بعداً می‌توانی اینجا محدودیت دانلود، صف ویژه، یا لینک‌های اختصاصی بگذاری."
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
                "/unban USER_ID",
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
                "• ارسال همگانی",
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

    if data.startswith("q:"):
        _, quality, short_id = data.split(":", 2)
        url = url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، دوباره بفرستید.")
            return

        await query.message.delete()
        asyncio.create_task(download_and_send(query.message, user, url, quality))
        return

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

    try:
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
        await record_download(user.id, url=url, quality=quality)

    except Exception as e:
        logger.exception("خطا در دانلود")
        await record_error()
        err_text = str(e)[:300]
        try:
            await status_msg.edit_text(f"❌ خطا در دانلود:\n`{err_text}`", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await chat_msg.reply_text(f"❌ خطا در دانلود:\n{err_text}")

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
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("off", toggle_maintenance))
    app.add_handler(CommandHandler("on", toggle_maintenance))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            menu_text_handler
        )
    )

    # لینک‌ها بعد از منوها پردازش شوند
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    print("ربات روشن شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
