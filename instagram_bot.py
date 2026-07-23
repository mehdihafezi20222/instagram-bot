import os
import json
import uuid
import shutil
import time
import asyncio
import logging
import re
import mimetypes
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib.request import Request, urlopen

from cachetools import TTLCache
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    channel_username: str
    daily_limit: int
    max_concurrent_downloads: int
    max_file_size_mb: int
    cleanup_interval_seconds: int
    orphan_max_age_seconds: int
    ytdlp_cookies_file: str
    log_level: str
    maintenance_default: bool

def load_config() -> Config:
    raw_admins = os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    admin_ids = {int(x) for x in raw_admins if x.isdigit()}

    return Config(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        admin_ids=admin_ids,
        channel_username=os.environ.get("CHANNEL_USERNAME", "").strip(),
        daily_limit=int(os.environ.get("DAILY_LIMIT", "10")),
        max_concurrent_downloads=int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3")),
        max_file_size_mb=int(os.environ.get("MAX_FILE_SIZE_MB", "500")),
        cleanup_interval_seconds=int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "1800")),
        orphan_max_age_seconds=int(os.environ.get("ORPHAN_MAX_AGE_SECONDS", "3600")),
        ytdlp_cookies_file=os.environ.get("YTDLP_COOKIES_FILE", "").strip(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        maintenance_default=os.environ.get("MAINTENANCE_DEFAULT", "false").lower() == "true",
    )

CONFIG = load_config()

if not CONFIG.bot_token:
    raise RuntimeError("متغیر BOT_TOKEN در Railway تنظیم نشده است.")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, CONFIG.log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("instagram_downloader_bot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = "downloads"
STATS_FILE = "stats.json"
CREDIT_MESSAGE = "🙏 با تشکر از امپراطور جاودانه"   # تغییر داده شد

SUPPORTED_DOMAINS = [
    "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com",
    "facebook.com", "fb.watch"
]

def is_supported_host(netloc: str) -> bool:
    netloc = (netloc or "").lower().replace("www.", "")
    for domain in SUPPORTED_DOMAINS:
        if netloc == domain or netloc.endswith("." + domain):
            return True
    return False

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")
URL_RE = re.compile(r'(https?://[^\s<>()]+|www\.[^\s<>()]+)', re.IGNORECASE)

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

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# State / Storage
# ---------------------------------------------------------------------------
class BotState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.url_cache = TTLCache(maxsize=1000, ttl=3600)
        self.quality_cache = TTLCache(maxsize=1000, ttl=3600)
        self.preview_cache = TTLCache(maxsize=500, ttl=900)
        self.request_cache = TTLCache(maxsize=500, ttl=86400)
        self.download_semaphore = asyncio.Semaphore(CONFIG.max_concurrent_downloads)
        self.active_downloads = {}
        self.active_url_jobs = {}
        self.active_download_meta = {}
        self.stats = self.load_stats()

    def load_stats(self):
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.setdefault("total_downloads", 0)
                    data.setdefault("total_errors", 0)
                    data.setdefault("users", {})
                    data.setdefault("banned", [])
                    data.setdefault("maintenance", CONFIG.maintenance_default)
                    data.setdefault("broadcast_count", 0)
                    data.setdefault("last_broadcast_failed", [])
                    data.setdefault("telegram_cache", {})
                    data.setdefault("daily_limit", CONFIG.daily_limit)
                    return data
            except Exception:
                logger.exception("خطا در خواندن stats.json")
        return {
            "total_downloads": 0,
            "total_errors": 0,
            "users": {},
            "banned": [],
            "maintenance": CONFIG.maintenance_default,
            "broadcast_count": 0,
            "last_broadcast_failed": [],
            "telegram_cache": {},
            "daily_limit": CONFIG.daily_limit,
        }

    def save_stats(self):
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("خطا در ذخیره stats.json")

    def clear_runtime_cache(self):
        self.url_cache.clear()
        self.quality_cache.clear()
        self.preview_cache.clear()
        self.request_cache.clear()

STATE = BotState()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")

def is_admin(user_id: int) -> bool:
    return user_id in CONFIG.admin_ids

def get_daily_limit() -> int:
    try:
        return int(STATE.stats.get("daily_limit", CONFIG.daily_limit))
    except Exception:
        return CONFIG.daily_limit

def get_user_record(user_id: int):
    uid = str(user_id)
    rec = STATE.stats["users"].setdefault(uid, deepcopy(DEFAULT_USER))
    for k, v in DEFAULT_USER.items():
        rec.setdefault(k, v)
    if not isinstance(rec.get("sites"), dict):
        rec["sites"] = {}
    if not isinstance(rec.get("history"), list):
        rec["history"] = []
    return rec

async def record_user(user_id: int, username: str):
    async with STATE.lock:
        uid = str(user_id)
        if uid not in STATE.stats["users"]:
            STATE.stats["users"][uid] = deepcopy(DEFAULT_USER)
            STATE.stats["users"][uid]["username"] = username or ""
        else:
            if username:
                STATE.stats["users"][uid]["username"] = username
            for k, v in DEFAULT_USER.items():
                STATE.stats["users"][uid].setdefault(k, v)
        STATE.stats["users"][uid]["last_seen"] = now_iso()
        STATE.save_stats()

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
    waiters = getattr(STATE.download_semaphore, "_waiters", None)
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
    STATE.save_stats()

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
        if not is_supported_host(netloc):
            return ""
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for k in ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "igsh", "fbclid", "si"]:
            query.pop(k, None)
        if "instagram.com" in netloc or "tiktok.com" in netloc:
            query = {}
        parsed = parsed._replace(query=urlencode(query, doseq=True), fragment="")
        return urlunparse(parsed)
    except Exception:
        return ""

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

def site_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        for k, v in SITE_LABELS.items():
            if k in netloc:
                return v
        return netloc or "Unknown"
    except Exception:
        return "Unknown"

def quality_to_format(quality: str):
    q = str(quality).strip().lower()
    if q == "image":
        return None
    if q == "audio":
        return "bestaudio/best"
    if q == "best":
        return "bv*+ba/b"
    if q.isdigit():
        h = int(q)
        return f"bv*[height<={h}]+ba/b[height<={h}]/best"
    return "bv*+ba/b"

def quality_label(quality: str) -> str:
    q = str(quality).strip().lower()
    if q == "image":
        return "عکس"
    if q == "audio":
        return "فقط صدا (MP3)"
    if q == "best":
        return "بهترین کیفیت"
    if q.isdigit():
        return f"{q}p"
    return {
        "1080": "1080p",
        "720": "720p",
        "480": "480p",
        "360": "360p",
    }.get(q, quality)

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
    cache = STATE.stats.get("telegram_cache", {})
    if not isinstance(cache, dict):
        STATE.stats["telegram_cache"] = {}
        return
    if len(cache) <= max_items:
        return
    items = sorted(cache.items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
    STATE.stats["telegram_cache"] = dict(items[:max_items])

def cache_key(url: str, quality: str) -> str:
    return f"{normalize_url(url)}||{quality}"

def get_cached_media(url: str, quality: str):
    ck = cache_key(url, quality)
    return STATE.stats.get("telegram_cache", {}).get(ck)

def set_cached_media(url: str, quality: str, data: dict):
    ck = cache_key(url, quality)
    STATE.stats.setdefault("telegram_cache", {})
    STATE.stats["telegram_cache"][ck] = data
    STATE.stats["telegram_cache"][ck]["updated_at"] = time.time()
    prune_telegram_cache(200)
    STATE.save_stats()

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

def get_available_heights(info: dict):
    return sorted(
        {
            f.get("height")
            for f in (info.get("formats") or [])
            if f.get("height")
        },
        reverse=True
    )

def is_image_only(info: dict) -> bool:
    if not info:
        return False

    ext = (info.get("ext") or "").lower().strip(".")

    if not info.get("formats") and not info.get("entries"):
        if f".{ext}" in IMAGE_EXTS:
            return True
        return not info.get("duration")

    formats = info.get("formats") or []
    if formats:
        has_video = any(f.get("vcodec") not in (None, "none") for f in formats)
        return not has_video

    entries = [e for e in (info.get("entries") or []) if e]
    if entries:
        return all(is_image_only(e) for e in entries)

    return False

def infer_download_mode(info: dict | None, url: str = "") -> str:
    if info:
        if is_image_only(info):
            return "image"
        ext = (info.get("ext") or "").lower().strip(".")
        if f".{ext}" in IMAGE_EXTS:
            return "image"
        if not info.get("duration") and not any(
            (f or {}).get("vcodec") not in (None, "none")
            for f in (info.get("formats") or [])
        ) and info.get("formats"):
            return "image"

    if url:
        try:
            path_ext = os.path.splitext(urlparse(url).path)[1].lower()
            if path_ext in IMAGE_EXTS:
                return "image"
        except Exception:
            pass

    return "video"


def _iter_media_url_candidates(value):
    if isinstance(value, dict):
        for key in ("url", "display_url", "thumbnail", "thumbnail_url", "original_url", "source_url", "webpage_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                yield candidate

        for child_key in ("entries", "formats", "requested_formats", "thumbnails"):
            child = value.get(child_key)
            if isinstance(child, list):
                for item in child:
                    yield from _iter_media_url_candidates(item)

        for child in value.values():
            if isinstance(child, dict):
                yield from _iter_media_url_candidates(child)
            elif isinstance(child, list):
                for item in child:
                    yield from _iter_media_url_candidates(item)

    elif isinstance(value, list):
        for item in value:
            yield from _iter_media_url_candidates(item)


def _guess_ext_from_url_or_type(url: str, content_type: str | None = None) -> str:
    try:
        path_ext = os.path.splitext(urlparse(url).path)[1].lower()
        if path_ext:
            return path_ext
    except Exception:
        pass

    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        guessed = mimetypes.guess_extension(ctype) or ""
        if guessed:
            return guessed

    return ".bin"


async def _download_direct_media_candidates(info: dict, job_dir: str, url: str) -> list[str]:
    candidates = []
    seen = set()
    for candidate in _iter_media_url_candidates(info or {}):
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    def _priority(u: str) -> tuple[int, str]:
        ext = os.path.splitext(urlparse(u).path)[1].lower()
        if ext in IMAGE_EXTS:
            return (0, u)
        if ext in VIDEO_EXTS:
            return (1, u)
        if ext in AUDIO_EXTS:
            return (2, u)
        return (3, u)

    candidates.sort(key=_priority)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": url,
        "Accept": "image/*,video/*,audio/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }

    saved_files = []

    def _download_one(candidate_url: str, index: int):
        req = Request(candidate_url, headers=headers)
        with urlopen(req, timeout=30) as resp:
            content_type = (resp.headers.get("Content-Type") if resp.headers else "") or ""
            ctype_main = content_type.split(";", 1)[0].strip().lower() if content_type else ""

            if ctype_main and not ctype_main.startswith(("image/", "video/", "audio/")):
                logger.debug("Content-Type نامناسب: %s", ctype_main)
                return None
            if not ctype_main:
                path_ext = os.path.splitext(urlparse(candidate_url).path)[1].lower()
                if not path_ext or path_ext not in IMAGE_EXTS + VIDEO_EXTS + AUDIO_EXTS:
                    logger.debug("پسوند نامشخص و Content-Type خالی")
                    return None

            ext = _guess_ext_from_url_or_type(candidate_url, content_type)
            out_name = f"direct_{index:02d}{ext}"
            out_path = os.path.join(job_dir, out_name)
            with open(out_path, "wb") as f:
                shutil.copyfileobj(resp, f)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            return None

    for idx, candidate_url in enumerate(candidates[:12], start=1):
        try:
            saved = await asyncio.to_thread(_download_one, candidate_url, idx)
            if saved and os.path.exists(saved) and os.path.getsize(saved) > 0:
                saved_files.append(saved)
        except Exception as e:
            logger.debug("خطا در دانلود مستقیم %s: %s", candidate_url, e)
            continue

    return saved_files

def get_preview_text(info: dict, url: str) -> str:
    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    size_bytes = estimate_size(info)
    heights = get_available_heights(info)
    top_heights = ", ".join(f"{h}p" for h in heights[:10]) if heights else "-"
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

async def is_channel_member(bot, user_id: int) -> bool:
    if not CONFIG.channel_username or is_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=CONFIG.channel_username, user_id=user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ]
    except Exception:
        logger.warning("خطا در بررسی عضویت کاربر %s در کانال", user_id)
        return True

async def fetch_preview_info(url: str):
    if url in STATE.preview_cache:
        return STATE.preview_cache[url]

    loop = asyncio.get_running_loop()

    def _probe():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "skip_download": True,
            "noplaylist": ("instagram.com" not in url.lower()) or ("img_index=" in url),
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if CONFIG.ytdlp_cookies_file and os.path.exists(CONFIG.ytdlp_cookies_file):
            opts["cookiefile"] = CONFIG.ytdlp_cookies_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        result = await loop.run_in_executor(None, _probe)
        if result:
            STATE.preview_cache[url] = result
        return result
    except Exception:
        return None

def detect_local_kind(fname: str) -> str:
    lower = fname.lower()
    if lower.endswith(IMAGE_EXTS):
        return "photo"
    if lower.endswith(AUDIO_EXTS):
        return "audio"
    if lower.endswith(VIDEO_EXTS):
        return "video"
    return "document"

async def _send_media_reply(message, kind: str, file_id: str, caption: str = ""):
    if kind == "photo":
        return await message.reply_photo(photo=file_id, caption=caption or None)
    if kind == "audio":
        return await message.reply_audio(audio=file_id, caption=caption or None)
    if kind == "video":
        return await message.reply_video(video=file_id, caption=caption or None, supports_streaming=True)
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

def register_active_job(key: str, job_id: str) -> bool:
    if key in STATE.active_url_jobs:
        return False
    STATE.active_url_jobs[key] = job_id
    return True

def unregister_active_job(key: str, job_id: str):
    if STATE.active_url_jobs.get(key) == job_id:
        STATE.active_url_jobs.pop(key, None)

def get_today_stats():
    today = date.today().isoformat()
    downloads_today = 0
    active_users_today = 0
    site_counts = {}

    for uid, rec in STATE.stats.get("users", {}).items():
        if rec.get("daily_date") == today and rec.get("daily_count", 0) > 0:
            downloads_today += rec.get("daily_count", 0)
            active_users_today += 1

        sites = rec.get("sites", {})
        if isinstance(sites, dict):
            for site, count in sites.items():
                site_counts[site] = site_counts.get(site, 0) + int(count or 0)

    top_site = "-"
    if site_counts:
        top_site = max(site_counts.items(), key=lambda x: x[1])[0]

    return {
        "downloads_today": downloads_today,
        "active_users_today": active_users_today,
        "site_counts": site_counts,
        "top_site": top_site,
    }

def find_user_record_by_id(user_id: int):
    return STATE.stats.get("users", {}).get(str(user_id))

def format_user_admin_card(user_id: int):
    rec = find_user_record_by_id(user_id)
    if not rec:
        return f"❌ کاربر با آیدی `{user_id}` پیدا نشد."

    total_size = human_size(rec.get("total_size", 0))
    level = get_level_text(rec.get("count", 0))
    fav_site = favorite_site(rec)
    limit_text = "نامحدود" if rec.get("vip") else str(get_daily_limit())

    return (
        f"👤 اطلاعات کاربر\n\n"
        f"ID: {user_id}\n"
        f"یوزرنیم: @{rec.get('username') or 'ندارد'}\n"
        f"دانلود: {rec.get('count', 0)}\n"
        f"حجم کل: {total_size}\n"
        f"سطح: {level}\n"
        f"سایت محبوب: {fav_site}\n"
        f"دانلود امروز: {rec.get('daily_count', 0)}/{limit_text}\n"
        f"VIP: {'فعال ⭐' if rec.get('vip') else 'غیرفعال'}\n"
        f"آخرین فعالیت: {rec.get('last_action', 'ندارد')}\n"
        f"آخرین سایت: {rec.get('last_site', 'ندارد')}\n"
        f"آخرین کیفیت: {quality_label(rec.get('last_quality', '')) if rec.get('last_quality') else 'ندارد'}"
    )

# ---------------------------------------------------------------------------
# Keyboard builders (ساده‌سازی شده)
# ---------------------------------------------------------------------------
def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["📥 دانلود", "👤 پروفایل"],
            ["📊 آمار", "⚙️ تنظیمات"],
            ["🛠 پنل ادمین"],
        ],
        resize_keyboard=True,
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
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار ربات", callback_data="admin_stats"),
            InlineKeyboardButton("📅 آمار امروز", callback_data="admin_today"),
        ],
        [
            InlineKeyboardButton("👥 کاربران", callback_data="admin_users"),
            InlineKeyboardButton("🔎 جستجوی کاربر", callback_data="admin_find_user"),
        ],
        [
            InlineKeyboardButton("📢 ارسال همگانی", callback_data="admin_broadcast"),
            InlineKeyboardButton("🚫 مدیریت کاربران", callback_data="admin_users_manage"),
        ],
        [
            InlineKeyboardButton("🔧 تنظیمات ربات", callback_data="admin_settings"),
            InlineKeyboardButton("🛠 تغییر حالت تعمیرات", callback_data="admin_maintenance"),
        ],
        [InlineKeyboardButton("❌ بستن پنل", callback_data="admin_close")],
    ])

def quality_keyboard(short_id: str, heights=None):
    buttons = []
    heights = heights or []
    unique_heights = []
    seen = set()

    for h in heights:
        if h and str(h).isdigit():
            h = int(h)
            if h not in seen:
                seen.add(h)
                unique_heights.append(h)

    unique_heights.sort(reverse=True)

    if unique_heights:
        for h in unique_heights:
            buttons.append([InlineKeyboardButton(f"📺 {h}p", callback_data=f"q:{h}:{short_id}")])
        buttons.append([InlineKeyboardButton("🎬 بهترین کیفیت", callback_data=f"q:best:{short_id}")])
    else:
        buttons.append([InlineKeyboardButton("🎬 بهترین کیفیت", callback_data=f"q:best:{short_id}")])

    buttons.append([InlineKeyboardButton("🎧 فقط صدا (MP3)", callback_data=f"q:audio:{short_id}")])
    buttons.append([InlineKeyboardButton("❌ انصراف", callback_data="cancel_select")])
    return InlineKeyboardMarkup(buttons)

def cancel_download_keyboard(job_id: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو دانلود", callback_data=f"cancel_dl:{job_id}")]])

def redo_keyboard(short_id: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 دانلود دوباره با کیفیت دیگه", callback_data=f"redo:{short_id}")]])

def force_join_keyboard():
    ch_clean = CONFIG.channel_username.replace("@", "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{ch_clean}")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join")]
    ])

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in STATE.stats.get("banned", []):
        await update.message.reply_text("🚫 شما مسدود شده‌اید.")
        return

    u = get_user_record(user.id)
    greeted = "خوش آمدید دوباره" if u.get("count", 0) else "خوش آمدید"
    await update.message.reply_text(
        f"سلام {user.first_name or 'دوست عزیز'} 👋\n{greeted}\n\n"
        "لینک پست یا ویدیو را بفرست تا گزینه‌های دانلود را نشان بدهم.",
        reply_markup=main_menu(),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنما:\n"
        "• لینک اینستاگرام، یوتیوب، تیک‌تاک، توییتر/X یا فیسبوک بفرست\n"
        "• می‌توانی چند لینک را در یک پیام بفرستی\n"
        "• از منو برای تنظیمات و امکانات بیشتر استفاده کن\n"
        "• ادمین‌ها می‌توانند از پنل ادمین استفاده کنند",
        reply_markup=main_menu(),
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(format_stats(user.id, user))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in CONFIG.admin_ids:
        await update.message.reply_text(
            f"⛔ دسترسی غیرمجاز!\nآیدی تلگرام شما: `{user_id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    active_running = len(STATE.active_downloads)
    cache_count = len(STATE.stats.get("telegram_cache", {}))
    today = get_today_stats()

    text = (
        "🛠 *پنل مدیریت ربات*\n\n"
        f"👥 تعداد کاربران: {len(STATE.stats['users'])}\n"
        f"📥 دانلودهای موفق: {STATE.stats['total_downloads']}\n"
        f"❌ مجموع خطاها: {STATE.stats['total_errors']}\n"
        f"🛠 حالت تعمیرات: {'فعال 🛠' if STATE.stats.get('maintenance') else 'غیرفعال 🟢'}\n"
        f"🚫 کاربران مسدود: {len(STATE.stats.get('banned', []))}\n"
        f"📢 ارسال همگانی انجام‌شده: {STATE.stats.get('broadcast_count', 0)}\n"
        f"⏳ دانلودهای فعال: {active_running}\n"
        f"🧠 کش فایل تلگرام: {cache_count}\n"
        f"📆 سقف دانلود روزانه: {get_daily_limit()}\n\n"
        f"📅 دانلودهای امروز: {today['downloads_today']}\n"
        f"👤 کاربران فعال امروز: {today['active_users_today']}\n"
        f"🔥 سایت محبوب امروز: {today['top_site']}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in CONFIG.admin_ids:
        await update.message.reply_text(
            f"⛔ دسترسی غیرمجاز!\nآیدی تلگرام شما: `{user_id}`",
            parse_mode=ParseMode.MARKDOWN,
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
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await update.message.reply_text("🚀 در حال ارسال پیام همگانی...")
    success, failed = 0, 0
    failed_ids = []

    for uid_str in list(STATE.stats["users"].keys()):
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

    async with STATE.lock:
        STATE.stats["broadcast_count"] = STATE.stats.get("broadcast_count", 0) + 1
        STATE.stats["last_broadcast_failed"] = failed_ids
        STATE.save_stats()

    report = f"📊 گزارش ارسال همگانی:\n\n✅ موفق: {success}\n❌ ناموفق: {failed}"
    if failed_ids:
        shown = ", ".join(failed_ids[:20])
        more = "" if len(failed_ids) <= 20 else f" (+{len(failed_ids) - 20} مورد دیگر)"
        report += f"\n\n?? آیدی‌های ناموفق:\n{shown}{more}"

    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

async def user_info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("استفاده: `/user USER_ID`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = int(context.args[0])
    await update.message.reply_text(format_user_admin_card(uid), parse_mode=ParseMode.MARKDOWN)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        if uid not in STATE.stats["banned"]:
            STATE.stats["banned"].append(uid)
            STATE.save_stats()
        await update.message.reply_text(f"🚫 کاربر {uid} مسدود شد.")
    else:
        await update.message.reply_text("استفاده: `/ban USER_ID`", parse_mode=ParseMode.MARKDOWN)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        if uid in STATE.stats["banned"]:
            STATE.stats["banned"].remove(uid)
            STATE.save_stats()
        await update.message.reply_text(f"✅ کاربر {uid} آزاد شد.")
    else:
        await update.message.reply_text("استفاده: `/unban USER_ID`", parse_mode=ParseMode.MARKDOWN)

async def toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    STATE.stats["maintenance"] = not STATE.stats.get("maintenance", False)
    STATE.save_stats()
    await update.message.reply_text(f"⚙️ حالت تعمیرات: {'فعال 🛠' if STATE.stats['maintenance'] else 'غیرفعال 🟢'}")

async def set_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        u = get_user_record(uid)
        u["vip"] = not u.get("vip", False)
        STATE.save_stats()
        await update.message.reply_text(f"⭐ وضعیت VIP کاربر {uid}: {'فعال ✅' if u['vip'] else 'غیرفعال ❌'}")
    else:
        await update.message.reply_text("استفاده: `/setvip USER_ID`", parse_mode=ParseMode.MARKDOWN)

async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    if context.args and context.args[0].isdigit():
        new_limit = int(context.args[0])
        STATE.stats["daily_limit"] = max(1, min(new_limit, 1000))
        STATE.save_stats()
        await update.message.reply_text(f"📆 سقف دانلود روزانه روی {STATE.stats['daily_limit']} تنظیم شد.")
    else:
        await update.message.reply_text("استفاده: `/limit 20`", parse_mode=ParseMode.MARKDOWN)

async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    top_n = 20
    if context.args and context.args[0].isdigit():
        top_n = max(5, min(int(context.args[0]), 50))
    users = []
    for uid, rec in STATE.stats["users"].items():
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
        display_uname = f"@{uname}" if uname not in ("-", "") else "-"
        lines.append(f"{i}. ID: {uid} | {display_uname} | دانلود: {cnt} | VIP: {'بله' if vip else 'خیر'}")
    await update.message.reply_text("\n".join(lines))

async def clearcache_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    STATE.clear_runtime_cache()
    STATE.stats["telegram_cache"] = {}
    STATE.save_stats()
    await update.message.reply_text("🧹 کش‌ها پاک شدند.")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_t = time.perf_counter()
    msg = await update.message.reply_text("🏓 Pong ...")
    elapsed = int((time.perf_counter() - start_t) * 1000)
    await msg.edit_text(f"🏓 Pong\n⏱ پاسخ: {elapsed}ms")

async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    if not STATE.active_downloads:
        await update.message.reply_text("📡 در حال حاضر دانلود فعالی وجود ندارد.")
        return
    lines = [f"📡 دانلودهای فعال ({len(STATE.active_downloads)}):\n"]
    for job_id in list(STATE.active_downloads.keys()):
        meta = STATE.active_download_meta.get(job_id, {})
        uid = meta.get("user_id", "-")
        uname = meta.get("username") or "-"
        url = meta.get("url", "-")
        quality = meta.get("quality", "-")
        started = meta.get("started", "-")
        lines.append(
            f"🆔 {job_id} | 👤 {uid} (@{uname})\n"
            f"🎚 {quality_label(quality)} | ⏰ شروع: {started}\n"
            f"🔗 {url[:70]}\n"
        )
    await update.message.reply_text("\n".join(lines))

async def ig_search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in STATE.stats.get("banned", []):
        return

    if not context.args:
        await update.message.reply_text(
            "استفاده: `/ig username` (بدون @)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    username = context.args[0].lstrip("@").strip()
    if not username or not re.match(r"^[A-Za-z0-9._]+$", username):
        await update.message.reply_text("⚠️ یوزرنیم نامعتبر است.")
        return

    status_msg = await update.message.reply_text(f"🔎 در حال جستجوی پست‌های @{username} ...")
    profile_url = f"https://www.instagram.com/{username}/"
    loop = asyncio.get_running_loop()

    def _probe():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "extract_flat": True,
            "playlistend": 10,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if CONFIG.ytdlp_cookies_file and os.path.exists(CONFIG.ytdlp_cookies_file):
            opts["cookiefile"] = CONFIG.ytdlp_cookies_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(profile_url, download=False)

    try:
        info = await loop.run_in_executor(None, _probe)
    except Exception:
        logger.exception("خطا در جستجوی اینستاگرام برای %s", username)
        info = None

    entries = [e for e in (info or {}).get("entries") or [] if e]

    if not entries:
        await status_msg.edit_text(
            "❌ نتیجه‌ای پیدا نشد.\n"
            "ممکن است پیج خصوصی باشد یا اینستاگرام دسترسی بدون‌لاگین را محدود کرده باشد."
        )
        return

    buttons = []
    for e in entries[:10]:
        post_url = e.get("url") or e.get("webpage_url")
        if not post_url:
            continue
        title = (e.get("title") or e.get("id") or "پست")[:30]
        short_id = uuid.uuid4().hex[:8]
        STATE.url_cache[short_id] = post_url
        buttons.append([InlineKeyboardButton(f"⬇️ {title}", callback_data=f"redo:{short_id}")])

    if not buttons:
        await status_msg.edit_text("❌ لینک قابل دانلودی در پست‌های این پیج پیدا نشد.")
        return

    await status_msg.edit_text(
        f"📸 آخرین پست‌های @{username}:\nروی هرکدام بزن تا کیفیت انتخاب کنی.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# ---------------------------------------------------------------------------
# Download flow
# ---------------------------------------------------------------------------
async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await record_user(user.id, user.username)

    if user.id in STATE.stats.get("banned", []):
        return

    if STATE.stats.get("maintenance", False) and user.id not in CONFIG.admin_ids:
        await update.message.reply_text("🛠 ربات در حال تعمیرات است.")
        return

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "⚠️ ابتدا در کانال ما عضو شوید:",
            reply_markup=force_join_keyboard(),
        )
        return

    raw_text = (update.message.text or update.message.caption or "").strip()
    if not raw_text:
        return

    urls = extract_urls(raw_text)
    if not urls:
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

# ========== تغییر اصلی: دانلود خودکار عکس بدون منوی کیفیت ==========
async def process_single_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    user = update.effective_user
    if STATE.stats.get("maintenance", False) and user.id not in CONFIG.admin_ids:
        return

    if not check_daily_limit(user.id):
        await update.message.reply_text(
            f"⛔ شما به سقف دانلود روزانه ({get_daily_limit()} مورد) رسیدید.\n"
            "فردا دوباره امتحان کنید یا برای دسترسی نامحدود VIP بگیرید."
        )
        return

    has_cookies = bool(CONFIG.ytdlp_cookies_file and os.path.exists(CONFIG.ytdlp_cookies_file))
    if "/stories/" in urlparse(url).path.lower() and not has_cookies:
        await update.message.reply_text(
            "⚠️ دانلود استوری اینستاگرام نیاز به کوکی لاگین دارد؛ اینستاگرام این بخش را "
            "حتی برای کاربران عمومی بدون لاگین بسته است.\n"
            "این محدودیت خود اینستاگرام است، نه مشکل ربات. لینک‌های پست، ریلز و ویدیو عادی کار می‌کنند."
        )
        return

    short_id = uuid.uuid4().hex[:8]
    STATE.url_cache[short_id] = url

    status_msg = await update.message.reply_text("🔎 در حال بررسی لینک...")

    preview_info = await fetch_preview_info(url)

    # --- اگر پست عکسی است، مستقیماً دانلود کن (بدون نمایش منوی کیفیت) ---
    if infer_download_mode(preview_info, url) == "image":
        try:
            await status_msg.edit_text("🖼 این یک پست عکسی است، در حال دانلود...")
        except Exception:
            pass

        ckey = cache_key(url, "best")   # کیفیت best برای عکس
        job_id = uuid.uuid4().hex[:8]
        if not register_active_job(ckey, job_id):
            await status_msg.edit_text("⚠️ این لینک الان در حال پردازش است.")
            return

        STATE.active_download_meta[job_id] = {
            "user_id": user.id,
            "username": user.username or "",
            "url": url,
            "quality": "best",        # بهترین کیفیت عکس
            "started": now_iso(),
        }
        task = asyncio.create_task(
            download_and_send(status_msg, user, url, "best", job_id, short_id, preview_info=preview_info)
        )
        STATE.active_downloads[job_id] = task
        return
    # --- پایان بخش عکس ---

    # برای ویدیوها و سایر محتواها، منوی کیفیت نمایش داده شود
    heights = []
    if preview_info:
        heights = get_available_heights(preview_info)
        STATE.quality_cache[short_id] = heights
        preview_text = get_preview_text(preview_info, url)
        try:
            await status_msg.edit_text(preview_text, reply_markup=quality_keyboard(short_id, heights))
        except Exception:
            await status_msg.reply_text(preview_text, reply_markup=quality_keyboard(short_id, heights))
        return

    try:
        await status_msg.edit_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id, heights))
    except Exception:
        await status_msg.reply_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id, heights))

# ========== پایان تغییر ==========

async def download_and_send(status_msg, user, url, quality, job_id, short_id, preview_info=None):
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
            await status_msg.reply_text("دوست داری با کیفیت دیگه‌ای هم دانلود کنی؟", reply_markup=redo_keyboard(short_id))
            await record_download(
                user.id,
                url=url,
                quality=quality,
                size_bytes=cache_hit.get("size", 0),
                site=site_from_url(url),
                title=cache_hit.get("title", ""),
            )
            increment_daily_count(user.id)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        await _download_and_send_real(status_msg, user, url, quality, job_id, short_id, job_dir, preview_info=preview_info)

    finally:
        unregister_active_job(ckey, job_id)
        STATE.active_downloads.pop(job_id, None)
        STATE.active_download_meta.pop(job_id, None)
        for f in os.listdir(job_dir):
            try:
                os.remove(os.path.join(job_dir, f))
            except Exception:
                pass
        try:
            os.rmdir(job_dir)
        except Exception:
            pass


async def _download_and_send_real(status_msg, user, url, quality, job_id, short_id, job_dir, preview_info=None):
    if preview_info is None:
        preview_info = await fetch_preview_info(url)

    inferred_mode = infer_download_mode(preview_info, url)
    is_image = str(quality).strip().lower() == "image" or inferred_mode == "image"

    if is_image and str(quality).strip().lower() == "audio":
        try:
            await status_msg.edit_text("⚠️ این پست فقط عکس است و خروجی صوتی ندارد.", reply_markup=None)
        except Exception:
            pass
        return

    ydl_opts = {
        "outtmpl": f"{job_dir}/%(id)s_%(autonumber)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noplaylist": ("instagram.com" not in url.lower()) or ("img_index=" in url),
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]},
        },
    }

    fmt = "best" if is_image else quality_to_format(quality)
    if fmt:
        ydl_opts["format"] = fmt
    if not is_image:
        ydl_opts["merge_output_format"] = "mp4"

    if CONFIG.ytdlp_cookies_file and os.path.exists(CONFIG.ytdlp_cookies_file):
        ydl_opts["cookiefile"] = CONFIG.ytdlp_cookies_file

    if quality == "audio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    loop = asyncio.get_running_loop()

    if STATE.download_semaphore.locked():
        pos = queue_waiting_count() + 1
        try:
            await status_msg.edit_text(
                f"⏳ در صف دانلود هستید... ({pos} نفر جلوتر از شما)",
                reply_markup=cancel_download_keyboard(job_id),
            )
        except Exception:
            pass

    async with STATE.download_semaphore:
        for attempt in range(1, 4):
            try:
                try:
                    label = "🖼 دانلود عکس شروع شد" if is_image else f"⬇️ دانلود شروع شد\n🎚 کیفیت: {quality_label(quality)}"
                    await status_msg.edit_text(
                        f"{label}\n⏳ لطفاً شکیبا باشید.",
                        reply_markup=cancel_download_keyboard(job_id),
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
                    fallback_files = await _download_direct_media_candidates(info, job_dir, url)
                    if fallback_files:
                        files = fallback_files
                    else:
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
                await status_msg.reply_text("دوست داری با کیفیت دیگه‌ای هم دانلود کنی؟", reply_markup=redo_keyboard(short_id))
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
                logger.exception("خطا در دانلود (attempt %s)", attempt)

                err_lower = str(e).lower()
                is_format_error = (
                    "requested format is not available" in err_lower
                    or "no video formats found" in err_lower
                    or "unable to extract" in err_lower and "format" in err_lower
                )
                if is_format_error and "format" in ydl_opts:
                    logger.info("خطای فرمت شناسایی شد؛ تلاش دوباره بدون فرمت ویدیویی (احتمالاً پست عکسی است)")
                    ydl_opts.pop("format", None)
                    ydl_opts.pop("merge_output_format", None)
                    ydl_opts.pop("postprocessors", None)
                    try:
                        await status_msg.edit_text(
                            "🖼 به‌نظر می‌رسه این یک پست عکسی باشه، دوباره امتحان می‌کنم...",
                            reply_markup=cancel_download_keyboard(job_id),
                        )
                    except Exception:
                        pass
                    continue

                async with STATE.lock:
                    STATE.stats["total_errors"] += 1
                    STATE.save_stats()

                if attempt < 3:
                    try:
                        await status_msg.edit_text(
                            f"⚠️ خطا در دانلود. تلاش مجدد {attempt + 1}/3 ...",
                            reply_markup=cancel_download_keyboard(job_id),
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
                            reply_markup=None,
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
    async with STATE.lock:
        STATE.stats["total_downloads"] += 1
        uid = str(user_id)
        if uid in STATE.stats["users"]:
            rec = STATE.stats["users"][uid]
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
        STATE.save_stats()

# ---------------------------------------------------------------------------
# Cleanup
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
            if now - mtime > CONFIG.orphan_max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
                logger.info("پوشه یتیم پاکسازی شد: %s", path)
    except Exception:
        logger.exception("خطا در پاکسازی خودکار")

# ---------------------------------------------------------------------------
# Callback handler (بخش‌های نمایشی حذف شده‌اند)
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
                STATE.save_stats()
                await query.message.edit_text(
                    "✅ عضویت شما تایید شد. ممنون که به ما پیوستی 🙌\nحالا می‌تونی لینک بفرستی."
                )
            else:
                await query.message.edit_text("✅ عضویت شما تایید شد. حالا می‌تونی لینک بفرستی.")
        else:
            await query.answer("⚠️ هنوز عضو کانال نشده‌اید.", show_alert=True)
        return

    # تنظیمات
    if data.startswith("settings_"):
        u = get_user_record(user.id)

        if data == "settings_lang":
            current = u.get("language", "fa")
            new_lang = "en" if current == "fa" else "fa"
            u["language"] = new_lang
            STATE.save_stats()
            await query.message.edit_text(f"🌐 زبان تغییر کرد: {new_lang}")
            return

        if data == "settings_dark":
            u["dark"] = not u.get("dark", False)
            STATE.save_stats()
            await query.message.edit_text(f"🌙 حالت شب: {'فعال ✅' if u['dark'] else 'خاموش ❌'}")
            return

        if data == "settings_notify":
            u["notify"] = not u.get("notify", True)
            STATE.save_stats()
            await query.message.edit_text(f"🔔 اعلان‌ها: {'فعال ✅' if u['notify'] else 'خاموش ❌'}")
            return

        if data == "settings_reset":
            u["notify"] = True
            u["dark"] = False
            u["language"] = "fa"
            STATE.save_stats()
            await query.message.edit_text("♻️ تنظیمات به حالت پیش‌فرض برگشت.")
            return

    # پنل ادمین
    if data.startswith("admin_"):
        if user.id not in CONFIG.admin_ids:
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return

        if data == "admin_stats":
            await query.message.edit_text(
                "📊 آمار ربات\n\n"
                f"👥 کاربران: {len(STATE.stats['users'])}\n"
                f"📥 دانلود موفق: {STATE.stats['total_downloads']}\n"
                f"❌ خطاها: {STATE.stats['total_errors']}\n"
                f"🚫 بن شده: {len(STATE.stats['banned'])}\n"
                f"🛠 حالت تعمیرات: {'فعال' if STATE.stats.get('maintenance') else 'غیرفعال'}\n"
                f"📢 ارسال همگانی: {STATE.stats.get('broadcast_count', 0)}\n"
                f"⏳ دانلودهای فعال: {len(STATE.active_downloads)}\n"
                f"🧠 کش فایل تلگرام: {len(STATE.stats.get('telegram_cache', {}))}\n"
                f"📆 سقف دانلود روزانه: {get_daily_limit()}",
                reply_markup=admin_keyboard(),
            )
            return

        if data == "admin_today":
            today = get_today_stats()
            await query.message.edit_text(
                "📅 آمار امروز\n\n"
                f"📥 دانلودهای امروز: {today['downloads_today']}\n"
                f"👤 کاربران فعال امروز: {today['active_users_today']}\n"
                f"🔥 سایت محبوب امروز: {today['top_site']}",
                reply_markup=admin_keyboard(),
            )
            return

        if data == "admin_users":
            await query.message.edit_text(
                f"👥 تعداد کاربران ثبت‌شده: {len(STATE.stats['users'])}",
                reply_markup=admin_keyboard(),
            )
            return

        if data == "admin_find_user":
            await query.message.edit_text(
                "🔎 برای دیدن اطلاعات کاربر از دستور زیر استفاده کن:\n\n"
                "`/user USER_ID`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard(),
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
                "/user USER_ID\n"
                "/clearcache\n"
                "/ping\n"
                "/live (دانلودهای زنده)\n"
                "/ig username (جستجوی اینستاگرام)",
                reply_markup=admin_keyboard(),
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
                reply_markup=admin_keyboard(),
            )
            return

        if data == "admin_maintenance":
            STATE.stats["maintenance"] = not STATE.stats.get("maintenance", False)
            STATE.save_stats()
            status = "فعال 🛠" if STATE.stats["maintenance"] else "غیرفعال 🟢"
            await query.message.edit_text(f"حالت تعمیرات: {status}", reply_markup=admin_keyboard())
            return

        if data == "admin_broadcast":
            await query.message.edit_text(
                "📢 ارسال همگانی\n\n"
                "از دستور زیر استفاده کن:\n\n"
                "`/broadcast متن پیام`\n\n"
                "یا روی یک پیام ریپلای کن و `/broadcast` بزن.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard(),
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
        url = STATE.url_cache.get(short_id)
        if not url:
            await query.message.edit_text("⚠️ لینک منقضی شده، لینک رو دوباره بفرست.")
            return
        heights = STATE.quality_cache.get(short_id, [])
        await query.message.edit_text("🎚 کیفیت مورد نظرت را انتخاب کن:", reply_markup=quality_keyboard(short_id, heights))
        return

    if data.startswith("cancel_dl:"):
        job_id = data.split(":", 1)[1]
        task = STATE.active_downloads.get(job_id)
        if task and not task.done():
            task.cancel()
        else:
            await query.answer("این دانلود قبلاً به پایان رسیده.", show_alert=True)
        return

    if data.startswith("q:"):
        _, quality, short_id = data.split(":", 2)
        url = STATE.url_cache.get(short_id)
        if not url:
            await query.message.reply_text("⚠️ لینک منقضی شده، دوباره بفرستید.")
            return

        if not check_daily_limit(user.id):
            await query.message.reply_text(f"⛔ شما به سقف دانلود روزانه ({get_daily_limit()} مورد) رسیدید.")
            return

        ckey = cache_key(url, quality)
        if ckey in STATE.active_url_jobs:
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
            reply_markup=cancel_download_keyboard(job_id),
        )
        STATE.active_download_meta[job_id] = {
            "user_id": user.id,
            "username": user.username or "",
            "url": url,
            "quality": quality,
            "started": now_iso(),
        }
        preview_info = await fetch_preview_info(url)
        task = asyncio.create_task(download_and_send(status_msg, user, url, quality, job_id, short_id, preview_info=preview_info))
        STATE.active_downloads[job_id] = task
        return

# ---------------------------------------------------------------------------
# Menu text handler
# ---------------------------------------------------------------------------
async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    await record_user(user.id, user.username)

    if text == "📥 دانلود":
        await update.message.reply_text("لینک پست یا ویدیو را بفرست تا گزینه‌های دانلود را نشان بدهم.")

    elif text == "👤 پروفایل":
        await update.message.reply_text(format_profile(user.id, user))

    elif text == "📊 آمار":
        await update.message.reply_text(format_stats(user.id, user))

    elif text == "⚙️ تنظیمات":
        await update.message.reply_text("⚙️ تنظیمات:", reply_markup=settings_keyboard())

    elif text == "🛠 پنل ادمین":
        await admin_panel(update, context)

    else:
        await handle_links(update, context)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(CONFIG.bot_token).build()

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
    app.add_handler(CommandHandler("user", user_info_cmd))
    app.add_handler(CommandHandler("clearcache", clearcache_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CommandHandler("ig", ig_search_cmd))
    app.add_handler(CommandHandler("off", toggle_maintenance))
    app.add_handler(CommandHandler("on", toggle_maintenance))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler))
    app.add_handler(MessageHandler(filters.CAPTION & ~filters.COMMAND, handle_links))

    if app.job_queue is not None:
        app.job_queue.run_repeating(cleanup_job, interval=CONFIG.cleanup_interval_seconds, first=60)
    else:
        logger.warning(
            "JobQueue فعال نیست؛ برای پاکسازی خودکار پکیج "
            "'python-telegram-bot[job-queue]' را نصب کنید."
        )

    print("ربات روشن شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
