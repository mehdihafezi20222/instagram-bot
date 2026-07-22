
import os
import json
import uuid
import shutil
import time
import asyncio
import logging
import re
from datetime import date, datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from collections import Counter
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

# کش‌ها
url_cache = TTLCache(maxsize=1000, ttl=3600)          # short_id -> url
preview_cache = TTLCache(maxsize=500, ttl=900)        # url -> preview info
request_cache = TTLCache(maxsize=500, ttl=86400)      # cache_key -> cached result meta (fallback in RAM)

# محدودیت تعداد دانلود همزمان
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# دانلودهای در حال اجرا
active_downloads = {}            # job_id -> asyncio.Task
active_url_jobs = {}             # cache_key -> job_id

# سقف دانلود روزانه
DEFAULT_DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "10"))

# پیام تشکر
CREDIT_MESSAGE = "🙏 با تشکر از امپراطور ۲۸"

# چند ثانیه یک پوشه‌ی دانلود یتیم روی دیسک باقی بماند قبل از پاکسازی خودکار
ORPHAN_MAX_AGE_SECONDS = 3600

SUPPORTED_DOMAINS = [
    "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "facebook.com", "fb.watch"
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")

URL_RE = re.compile(r'(https?://[^\s<>()]+|www\.[^\s<>()]+)', re.IGNORECASE)

MENU_TEXTS = {
    "📥 دانلود", "👤 پروفایل", "⚙️ تنظیمات", "🤝 تعامل",
    "🔔 اعلان‌ها", "🚀 امکانات حرفه‌ای", "🛠 پنل ادمین", "💬 پشتیبانی",
}

SITE_LABELS = {
    "instagram.com": "Instagram",
    "youtu.be": "YouTube",
    "youtube.com": "YouTube",
    "twitter.com": "X/Twitter",
    "x.com": "X/Twitter",
    "tiktok.com": "TikTok",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
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
                data.setdefault("telegram_cache", {})
                data.setdefault("daily_limit", DEFAULT_DAILY_LIMIT)
                return data
        except Exception:
            logger.exception("خطا در خواندن stats.json")
    return {
        "total_downloads": 0,
        "total_errors": 0,
        "users": {},
        "banned": [],
        "maintenance": False,
        "broadcast_count": 0,
        "last_broadcast_failed": [],
        "telegram_cache": {},
        "daily_limit": DEFAULT_DAILY_LIMIT,
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
    "total_size": 0,
    "sites": {},
    "last_seen": "",
    "last_site": "",
    "last_url": "",
    "last_quality": "",
    "last_title": "",
}

# ---------------------------------------------------------------------------
# ابزارهای کمکى
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_daily_limit() -> int:
    try:
        return int(stats.get("daily_limit", DEFAULT_DAILY_LIMIT))
    except Exception:
        return DEFAULT_DAILY_LIMIT

def get_user_record(user_id: int):
    uid = str(user_id)
    rec = stats["users"].setdefault(uid, dict(DEFAULT_USER))
    for k, v in DEFAULT_USER.items():
        rec.setdefault(k, v)
    if not isinstance(rec.get("sites"), dict):
        rec["sites"] = {}
    if not isinstance(rec.get("history"), list):
        rec["history"] = []
    return rec

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
        stats["users"][uid]["last_seen"] = now_iso()
        save_stats(stats)

def human_size(num_bytes):
    try:
        num = float(num_bytes)
    except (TypeError, ValueError):
        return "نامشخص"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"

def queue_waiting_count():
    waiters = getattr(download_semaphore, "_waiters", None)
    try:
        return len(waiters) if waiters else 0
    except Exception:
        return 0

def check_daily_limit(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    u = get_user_record(user_id)
    if u.get("vip"):
        return True
    today = date.today().isoformat()
    if u.get("daily_date") != today:
        return True
    return u.get("daily_count", 0) < get_daily_limit()

def increment_daily_count(user_id: int):
    u = get_user_record(user_id)
    today = date.today().isoformat()
    if u.get("daily_date") != today:
        u["daily_date"] = today
        u["daily_count"] = 0
    u["daily_count"] = u.get("daily_count", 0) + 1
    save_stats(stats)

def extract_urls(text: str):
    if not text:
        return []
    raw = [m.group(0).rstrip(").,]}'\"") for m in URL_RE.finditer(text)]
    cleaned = []
    seen = set()
    for u in raw:
        nu = normalize_url(u)
        if nu and nu not in seen:
            seen.add(nu)
            cleaned.append(nu)
    return cleaned

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("www."):
        url = "https://" + url
    elif not url.startswith(("http://", "https://")) and any(d in url.lower() for d in SUPPORTED_DOMAINS):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().replace("www.", "")
        if not any(d in netloc for d in SUPPORTED_DOMAINS):
            return ""
        # برخی پارامترهای تبلیغاتی و مزاحم حذف شوند
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for k in ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "igsh", "fbclid", "si"]:
            query.pop(k, None)
        if "instagram.com" in netloc or "tiktok.com" in netloc:
            query = {}
        parsed = parsed._replace(query=urlencode(query, doseq=True), fragment="")
        return urlunparse(parsed)
    except Exception:
        return ""

def site_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        for k, v in SITE_LABELS.items():
            if k in netloc:
                return v
        return netloc or "Unknown"
    except Exception:
        return "Unknown"

def quality_to_format(quality: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    if quality == "1080":
        return "bv*[height<=1080]+ba/b[height<=1080]"
    if quality == "720":
        return "bv*[height<=720]+ba/b[height<=720]"
    if quality == "480":
        return "bv*[height<=480]+ba/b[height<=480]"
    if quality == "360":
        return "bv*[height<=360]+ba/b[height<=360]"
    return "bv*+ba/b"

def quality_label(quality: str) -> str:
    return {
        "best": "بهترین کیفیت",
        "1080": "1080p",
        "720": "720p",
        "480": "480p",
        "360": "360p",
        "audio": "فقط صدا (MP3)",
    }.get(quality, quality)

def get_level_text(download_count: int) -> str:
    if download_count >= 300:
        return "👑 امپراطور"
    if download_count >= 100:
        return "🥇 حرفه‌ای"
    if download_count >= 20:
        return "🥈 فعال"
    return "🥉 تازه‌کار"

def favorite_site(rec: dict) -> str:
    sites = rec.get("sites", {})
    if not isinstance(sites, dict) or not sites:
        return "-"
    return max(sites.items(), key=lambda x: x[1])[0]

def format_profile(user_id: int, user) -> str:
    u = get_user_record(user_id)
    total_size = human_size(u.get("total_size", 0))
    fav_site = favorite_site(u)
    level = get_level_text(u.get("count", 0))
    limit_text = "نامحدود" if (u.get("vip") or is_admin(user_id)) else str(get_daily_limit())
    return (
        f"👤 پروفایل شما\n\n"
        f"نام: {user.first_name or '-'}\n"
        f"یوزرنیم: @{user.username if user.username else 'ندارد'}\n"
        f"آیدی: {user.id}\n"
        f"تعداد دانلود: {u.get('count', 0)}\n"
        f"حجم کل دانلود: {total_size}\n"
        f"سطح: {level}\n"
        f"سایت محبوب: {fav_site}\n"
        f"دانلود امروز: {u.get('daily_count', 0)}/{limit_text}\n"
        f"اعلان‌ها: {'فعال' if u.get('notify', True) else 'خاموش'}\n"
        f"حالت شب: {'فعال' if u.get('dark', False) else 'خاموش'}\n"
        f"زبان: {u.get('language', 'fa')}\n"
        f"VIP: {'فعال ⭐' if u.get('vip') else 'غیرفعال'}"
    )

def format_stats(user_id: int, user) -> str:
    u = get_user_record(user_id)
    total_size = human_size(u.get("total_size", 0))
    fav_site = favorite_site(u)
    level = get_level_text(u.get("count", 0))
    return (
        f"📊 آمار شما\n\n"
        f"👤 نام: {user.first_name or '-'}\n"
        f"📥 دانلودها: {u.get('count', 0)}\n"
        f"📦 حجم کل: {total_size}\n"
        f"⭐ سطح: {level}\n"
        f"🌐 سایت محبوب: {fav_site}\n"
        f"📆 دانلود امروز: {u.get('daily_count', 0)}/{ 'نامحدود' if (u.get('vip') or is_admin(user_id)) else get_daily_limit() }\n"
        f"🕘 آخرین فعالیت: {u.get('last_action', 'ندارد')}\n"
        f"🔔 اعلان‌ها: {'فعال' if u.get('notify', True) else 'خاموش'}\n"
        f"🌙 حالت شب: {'فعال' if u.get('dark', False) else 'خاموش'}"
    )

def prune_telegram_cache(max_items: int = 200):
    cache = stats.get("telegram_cache", {})
    if not isinstance(cache, dict):
        stats["telegram_cache"] = {}
        return
    if len(cache) <= max_items:
        return
    items = sorted(cache.items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
    stats["telegram_cache"] = dict(items[:max_items])

def cache_key(url: str, quality: str) -> str:
    return f"{normalize_url(url)}||{quality}"

def get_cached_media(url: str, quality: str):
    ck = cache_key(url, quality)
    return stats.get("telegram_cache", {}).get(ck)

def set_cached_media(url: str, quality: str, data: dict):
    ck = cache_key(url, quality)
    stats.setdefault("telegram_cache", {})
    stats["telegram_cache"][ck] = data
    stats["telegram_cache"][ck]["updated_at"] = time.time()
    prune_telegram_cache(200)
    save_stats(stats)

def clear_runtime_cache():
    url_cache.clear()
    preview_cache.clear()
    request_cache.clear()

def get_preview_text(info: dict, url: str) -> str:
    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    size_bytes = estimate_size(info)
    heights = sorted({f.get("height") for f in (info.get("formats") or []) if f.get("height")}, reverse=True)
    top_heights = ", ".join(f"{h}p" for h in heights[:5]) if heights else "-"
    lines = []
    if title:
        lines.append(f"🎬 {title[:150]}")
    lines.append(f"🔗 {site_from_url(url)}")
    if duration:
        try:
            mins, secs = divmod(int(duration), 60)
            hours, mins = divmod(mins, 60)
            dur = f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"
            lines.append(f"⏱ مدت: {dur}")
        except Exception:
            pass
    if size_bytes:
        lines.append(f"📦 حجم تقریبی: {human_size(size_bytes)}")
    lines.append(f"🎚 کیفیت‌های پیدا شده: {top_heights}")
    lines.append("یک کیفیت را انتخاب کن:")
    return "\n".join(lines)

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
        logger.warning("خطا در بررسی عضویت کاربر %s در کانال", user_id)
        return True

async def fetch_preview_info(url: str):
    if url in preview_cache:
        return preview_cache[url]
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
        cookiefile = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
        if cookiefile and os.path.exists(cookiefile):
            opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        result = await loop.run_in_executor(None, _probe)
        if result:
            preview_cache[url] = result
        return result
    except Exception:
        return None

def send_cached_response_helper(message, item: dict, caption: str = ""):
    return _send_media_reply(message, item["kind"], item["file_id"], caption or item.get("caption") or "")

async def _send_media_reply(message, kind: str, file_id: str, caption: str = ""):
    if kind == "photo":
        return await message.reply_photo(photo=file_id, caption=caption or None)
    if kind == "audio":
        return await message.reply_audio(audio=file_id, caption=caption or None)
    if kind == "video":
        return await message.reply_video(video=file_id, caption=caption or None, supports_streaming=True)
    if kind == "document":
        return await message.reply_document(document=file_id, caption=caption or None)
    return await message.reply_document(document=file_id, caption=caption or None)

async def _send_local_file(message, fname: str, caption: str = "", send_caption: bool = True):
    lower = fname.lower()
    with open(fname, "rb") as f:
        if lower.endswith(IMAGE_EXTS):
            return await message.reply_photo(photo=f, caption=caption if send_caption else None)
        if lower.endswith(AUDIO_EXTS):
            return await message.reply_audio(audio=f, caption=caption if send_caption else None)
        if lower.endswith(VIDEO_EXTS):
            return await message.reply_video(video=f, caption=caption if send_caption else None, supports_streaming=True)
        return await message.reply_document(document=f, caption=caption if send_caption else None)

def detect_local_kind(fname: str) -> str:
    lower = fname.lower()
    if lower.endswith(IMAGE_EXTS):
        return "photo"
    if lower.endswith(AUDIO_EXTS):
        return "audio"
    if lower.endswith(VIDEO_EXTS):
        return "video"
    return "document"

def register_active_job(key: str, job_id: str) -> bool:
    if key in active_url_jobs:
        return False
    active_url_jobs[key] = job_id
    return True

def unregister_active_job(key: str, job_id: str):
    if active_url_jobs.get(key) == job_id:
        active_url_jobs.pop(key, None)

# ---------------------------------------------------------------------------
# پیام‌های عمومی
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in stats.get("banned", []):
        await update.message.reply_text("🚫 شما مسدود شده‌اید.")
        return

    u = get_user_record(user.id)
    greeted = "خوش آمدید دوباره" if u.get("count", 0) else "خوش آمدید"
    await update.message.reply_text(
        f"سلام {user.first_name or 'دوست عزیز'} 👋\n"
        f"{greeted}\n\n"
        "لینک پست یا ویدیو را بفرست تا گزینه‌های دانلود را نشان بدهم.",
        reply_markup=main_menu()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنما:\n"
        "• لینک اینستاگرام، یوتیوب، تیک‌تاک، توییتر/X یا فیسبوک بفرست\n"
        "• می‌توانی چند لینک را در یک پیام بفرستی\n"
        "• از منو برای تنظیمات و امکانات بیشتر استفاده کن\n"
        "• ادمین‌ها می‌توانند از پنل ادمین استفاده کنند",
        reply_markup=main_menu()
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user_record(user.id)
    await update.message.reply_text(format_stats(user.id, user))

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

    active_running = len(active_downloads)
    cache_count = len(stats.get("telegram_cache", {}))
    text = (
        "🛠 *پنل مدیریت ربات*\n\n"
        f"👥 تعداد کاربران: {len(stats['users'])}\n"
        f"📥 دانلودهای موفق: {stats['total_downloads']}\n"
        f"❌ مجموع خطاها: {stats['total_errors']}\n"
        f"🛠 حالت تعمیرات: {'فعال 🛠' if stats.get('maintenance') else 'غیرفعال 🟢'}\n"
        f"🚫 کاربران مسدود: {len(stats.get('banned', []))}\n"
        f"📢 ارسال همگانی انجام‌شده: {stats.get('broadcast_count', 0)}\n"
        f"⏳ دانلودهای فعال: {active_running}\n"
        f"🧠 کش فایل تلگرام: {cache_count}\n"
        f"📆 سقف دانلود روزانه: {get_daily_limit()}"
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

async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if context.args and context.args[0].isdigit():
        new_limit = int(context.args[0])
        stats["daily_limit"] = max(1, min(new_limit, 1000))
        save_stats(stats)
        await update.message.reply_text(f"📆 سقف دانلود روزانه روی {stats['daily_limit']} تنظیم شد.")
    else:
        await update.message.reply_text("استفاده: `/limit 20`", parse_mode=ParseMode.MARKDOWN)

async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    top_n = 20
    if context.args and context.args[0].isdigit():
        top_n = max(5, min(int(context.args[0]), 50))
    users = []
    for uid, rec in stats["users"].items():
        users.append((
            int(uid),
            rec.get("count", 0),
            rec.get("username") or "-",
            rec.get("last_seen") or "-",
            rec.get("vip", False),
        ))
    users.sort(key=lambda x: x[1], reverse=True)
    if not users:
        await update.message.reply_text("هیچ کاربری ثبت نشده است.")
        return
    lines = [f"👥 کاربران ثبت‌شده (نمایش {min(top_n, len(users))} نفر اول):\n"]
    for i, (uid, cnt, uname, seen, vip) in enumerate(users[:top_n], start=1):
        display_uname = f'@{uname}' if uname not in ('-', '') else '-'
        lines.append(f"{i}. ID: {uid} | {display_uname} | دانلود: {cnt} | VIP: {'بله' if vip else 'خیر'}")
    await update.message.reply_text("\n".join(lines))

async def clearcache_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    clear_runtime_cache()
    stats["telegram_cache"] = {}
    save_stats(stats)
    await update.message.reply_text("🧹 کش‌ها پاک شدند.")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_t = time.perf_counter()
    msg = await update.message.reply_text("🏓 Pong ...")
    elapsed = int((time.perf_counter() - start_t) * 1000)
    await msg.edit_text(f"🏓 Pong\n⏱ پاسخ: {elapsed}ms")

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
            InlineKeyboardButton("🔁 آخرین دانلود", callback_data="last_download"),
            InlineKeyboardButton("⭐ VIP", callback_data="vip"),
        ],
        [
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
        [InlineKeyboardButton("🎬 بهترین کیفیت", callback_data=f"q:best:{short_id}")],
        [InlineKeyboardButton("📺 1080p", callback_data=f"q:1080:{short_id}")],
        [InlineKeyboardButton("📺 720p", callback_data=f"q:720:{short_id}")],
        [InlineKeyboardButton("📺 480p", callback_data=f"q:480:{short_id}")],
        [InlineKeyboardButton("📺 360p", callback_data=f"q:360:{short_id}")],
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
# پردازش لینک
# ---------------------------------------------------------------------------
async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    urls = extract_urls(raw_text)
    if not urls:
        # اگر متن صرفاً عادی بود، از منو محسوب نشده، پیام راهنما بده
        if update.message.text:
            await update.message.reply_text(
                "⚠️ لینک معتبر پیدا نشد یا از این سایت‌ها پشتیبانی نمی‌شود.\n"
                "سایت‌های پشتیبانی‌شده: اینستاگرام، یوتیوب، تیک‌تاک، توییتر/X، فیسبوک"
            )
        return

    if len(urls) > 1:
        await update.message.reply_text(f"🔎 {len(urls)} لینک پیدا شد. برای هرکدام جداگانه کیفیت انتخاب می‌شود.")

    for url in urls:
        await process_single_url(update, context, url)

async def process_single_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    user = update.effective_user
    if stats.get("maintenance", False) and user.id not in ADMIN_IDS:
        return

    if not check_daily_limit(user.id):
        await update.message.reply_text(
            f"⛔ شما به سقف دانلود روزانه ({get_daily_limit()} مورد) رسیدید.\n"
            "فردا دوباره امتحان کنید یا برای دسترسی نامحدود VIP بگیرید."
        )
        return

    short_id = uuid.uuid4().hex[:8]
    url_cache[short_id] = url

    status_msg = await update.message.reply_text("🔎 در حال بررسی لینک...")

    preview_info = await fetch_preview_info(url)
    if preview_info:
        title = (preview_info.get("title") or "").strip()
        size_bytes = estimate_size(preview_info)
        preview_text = get_preview_text(preview_info, url)
        if title:
            try:
                await status_msg.edit_text(
                    preview_text,
                    reply_markup=quality_keyboard(short_id)
                )
            except Exception:
                await status_msg.reply_text(
                    preview_text,
                    reply_markup=quality_keyboard(short_id)
                )
            return
        if size_bytes:
            try:
                await status_msg.edit_text(
                    preview_text,
                    reply_markup=quality_keyboard(short_id)
                )
            except Exception:
                await status_msg.reply_text(
                    preview_text,
                    reply_markup=quality_keyboard(short_id)
                )
            return

    try:
        await status_msg.edit_text(
            "🎚 کیفیت مورد نظرت را انتخاب کن:",
            reply_markup=quality_keyboard(short_id)
        )
    except Exception:
        await status_msg.reply_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id))

# ---------------------------------------------------------------------------
# هندلر دکمه‌ها
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "back_main":
        try:
            await query.message.edit_text("🏠 منوی اصلی", reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("یکی از گزینه‌ها را انتخاب کن:", reply_markup=main_menu())
        return

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

    if data == "my_stats":
        await query.message.edit_text(format_stats(user.id, user))
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
            title = item.get("title", "")
            lines.append(f"{i}. کیفیت: {q}\n{title}\n{url}\n")
        await query.message.edit_text("\n".join(lines))
        return

    if data == "last_download":
        u = get_user_record(user.id)
        if not u.get("last_url"):
            await query.message.edit_text("🕘 هنوز دانلودی ثبت نشده است.")
            return
        short_id = uuid.uuid4().hex[:8]
        url_cache[short_id] = u["last_url"]
        quality = u.get("last_quality") or "best"
        await query.message.edit_text(
            f"🔁 آخرین دانلود شما آماده است.\nکیفیت قبلی: {quality_label(quality)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 با همان کیفیت", callback_data=f"q:{quality}:{short_id}")],
                [InlineKeyboardButton("🎚 انتخاب کیفیت دیگر", callback_data=f"redo:{short_id}")]
            ])
        )
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
                f"📢 ارسال همگانی: {stats.get('broadcast_count', 0)}\n"
                f"⏳ دانلودهای فعال: {len(active_downloads)}\n"
                f"🧠 کش فایل تلگرام: {len(stats.get('telegram_cache', {}))}\n"
                f"📆 سقف دانلود روزانه: {get_daily_limit()}",
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
                "/setvip USER_ID\n"
                "/limit NUMBER\n"
                "/users [N]\n"
                "/clearcache\n"
                "/ping",
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
                f"• سقف دانلود روزانه: {get_daily_limit()}",
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
                f"⛔ شما به سقف دانلود روزانه ({get_daily_limit()} مورد) رسیدید."
            )
            return

        ckey = cache_key(url, quality)
        if ckey in active_url_jobs:
            await query.answer("⚠️ این لینک الان در حال پردازش است.", show_alert=True)
            return
        job_id = uuid.uuid4().hex[:8]
        if not register_active_job(ckey, job_id):
            await query.answer("⚠️ این لینک الان در حال پردازش است.", show_alert=True)
            return

        try:
            await query.message.delete()
        except Exception:
            pass

        status_msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="⏳ در حال آماده‌سازی دانلود... لطفاً شکیبا باشید.",
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

    ckey = cache_key(url, quality)
    cache_hit = get_cached_media(url, quality)

    try:
        if cache_hit and cache_hit.get("items"):
            try:
                await status_msg.edit_text("⚡ از کش تلگرام ارسال می‌شود...", reply_markup=None)
            except Exception:
                pass
            caption_used = False
            for item in cache_hit["items"]:
                caption = item.get("caption", "")
                if caption_used:
                    caption = ""
                await _send_media_reply(status_msg, item["kind"], item["file_id"], caption)
                caption_used = True

            await status_msg.reply_text(CREDIT_MESSAGE)
            await status_msg.reply_text(
                "دوست داری با کیفیت دیگه‌ای هم دانلود کنی؟",
                reply_markup=redo_keyboard(short_id)
            )
            await record_download(
                user.id,
                url=url,
                quality=quality,
                size_bytes=cache_hit.get("size", 0),
                site=site_from_url(url),
                title=cache_hit.get("title", "")
            )
            increment_daily_count(user.id)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # اگر کش نبود، دانلود واقعی انجام شود
        await _download_and_send_real(status_msg, user, url, quality, job_id, short_id, job_dir)

    finally:
        unregister_active_job(ckey, job_id)
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

async def _download_and_send_real(status_msg, user, url, quality, job_id, short_id, job_dir):
    ydl_opts = {
        'outtmpl': f'{job_dir}/%(id)s_%(autonumber)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'noplaylist': True,
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'merge_output_format': 'mp4',
        'format': quality_to_format(quality),
        'extractor_args': {
            'youtube': {'player_client': ['android', 'web']}
        },
    }

    cookiefile = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if cookiefile and os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile

    if quality == "audio":
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    loop = asyncio.get_running_loop()
    attempt_errors = []

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
        for attempt in range(1, 4):
            try:
                try:
                    await status_msg.edit_text(
                        f"⬇️ دانلود شروع شد\n🎚 کیفیت: {quality_label(quality)}\n⏳ لطفاً شکیبا باشید.",
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

                sent_items = []
                total_size = 0
                first_caption = True
                download_title = (info.get("title") or info.get("description") or "").strip()
                site_label = site_from_url(url)

                for fname in files:
                    lower = fname.lower()
                    kind = detect_local_kind(fname)
                    file_size = os.path.getsize(fname) if os.path.exists(fname) else 0
                    total_size += file_size

                    send_caption = "✅ دانلود شد!" if first_caption else ""
                    msg = await _send_local_file(status_msg, fname, send_caption=bool(first_caption), caption=send_caption)
                    first_caption = False

                    file_id = None
                    if kind == "photo" and getattr(msg, "photo", None):
                        file_id = msg.photo[-1].file_id
                    elif kind == "video" and getattr(msg, "video", None):
                        file_id = msg.video.file_id
                    elif kind == "audio" and getattr(msg, "audio", None):
                        file_id = msg.audio.file_id
                    elif kind == "document" and getattr(msg, "document", None):
                        file_id = msg.document.file_id

                    if file_id:
                        sent_items.append({
                            "kind": kind,
                            "file_id": file_id,
                            "caption": send_caption,
                        })

                caption_text = (info.get("description") or info.get("title") or "").strip()
                if caption_text:
                    await status_msg.reply_text(f"📝 کپشن:\n\n{caption_text[:1000]}")

                await status_msg.reply_text(CREDIT_MESSAGE)

                await status_msg.reply_text(
                    "دوست داری با کیفیت دیگه‌ای هم دانلود کنی؟",
                    reply_markup=redo_keyboard(short_id)
                )

                await status_msg.delete()

                if sent_items:
                    set_cached_media(url, quality, {
                        "items": sent_items,
                        "title": download_title,
                        "size": total_size,
                        "site": site_label,
                    })

                await record_download(
                    user.id,
                    url=url,
                    quality=quality,
                    size_bytes=total_size,
                    site=site_label,
                    title=download_title,
                )
                increment_daily_count(user.id)
                return

            except asyncio.CancelledError:
                try:
                    await status_msg.edit_text("❌ دانلود لغو شد.", reply_markup=None)
                except Exception:
                    pass
                raise

            except Exception as e:
                attempt_errors.append(str(e))
                logger.exception("خطا در دانلود (attempt %s)", attempt)
                await record_error()

                if attempt < 3:
                    try:
                        await status_msg.edit_text(
                            f"⚠️ خطا در دانلود. تلاش مجدد {attempt+1}/3 ...",
                            reply_markup=cancel_download_keyboard(job_id)
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                else:
                    err_text = str(e)[:300]
                    try:
                        await status_msg.edit_text(
                            f"❌ خطا در دانلود:\n`{err_text}`",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=None
                        )
                    except Exception:
                        await status_msg.reply_text(f"❌ خطا در دانلود:\n{err_text}")
                    try:
                        await status_msg.reply_text(
                            "📌 راهنمای سریع:\n"
                            "• لینک را دوباره بفرست\n"
                            "• اگر اینستاگرام است، گاهی خصوصی/محدود است\n"
                            "• اگر اینترنت ضعیف است دوباره تلاش کن"
                        )
                    except Exception:
                        pass
                    return

async def record_download(user_id: int, url: str = "", quality: str = "", size_bytes: int = 0, site: str = "", title: str = ""):
    async with stats_lock:
        stats["total_downloads"] += 1
        uid = str(user_id)
        if uid in stats["users"]:
            rec = stats["users"][uid]
            rec["count"] += 1
            rec["last_action"] = "download"
            rec["last_seen"] = now_iso()
            rec["last_url"] = url
            rec["last_quality"] = quality
            rec["last_site"] = site
            rec["last_title"] = title
            rec["total_size"] = int(rec.get("total_size", 0)) + int(size_bytes or 0)
            rec.setdefault("sites", {})
            rec["sites"][site] = int(rec["sites"].get(site, 0)) + 1
            history_item = {
                "url": url,
                "quality": quality,
                "site": site,
                "size": int(size_bytes or 0),
                "title": title[:200],
                "ts": now_iso(),
            }
            rec.setdefault("history", [])
            rec["history"].insert(0, history_item)
            rec["history"] = rec["history"][:10]
        save_stats(stats)

async def record_error():
    async with stats_lock:
        stats["total_errors"] += 1
        save_stats(stats)

# ---------------------------------------------------------------------------
# پاکسازی خودکار
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
    app.add_handler(CommandHandler("limit", set_limit))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("clearcache", clearcache_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("off", toggle_maintenance))
    app.add_handler(CommandHandler("on", toggle_maintenance))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            menu_text_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.CAPTION & ~filters.COMMAND,
            handle_links
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

async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    await record_user(user.id, user.username)

    if text == "📥 دانلود":
        await update.message.reply_text(
            "لینک پست یا ویدیو را بفرست تا گزینه‌های دانلود را نشان بدهم."
        )

    elif text == "👤 پروفایل":
        await update.message.reply_text(format_profile(user.id, user))

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
        await handle_links(update, context)


if __name__ == "__main__":
    main()
