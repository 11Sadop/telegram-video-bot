"""
بوت تحميل وتعديل الفيديوهات - تيليجرام
"""

import os
import re
import logging
import asyncio
import uuid
from pathlib import Path
from dotenv import load_dotenv

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from content_filter import (
    is_blocked_domain,
    check_video_metadata,
    check_transcript,
)
from nsfw_detector import check_video_frames
from transcriber import transcribe_video
from text_remover import remove_text_from_video

# ============= الإعدادات =============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("⚠️ لم يتم العثور على BOT_TOKEN في متغيرات البيئة")

# تحكم في الفلاتر (لتسريع البوت: يمكن تعطيل الفلاتر الثقيلة)
ENABLE_FRAME_CHECK = os.getenv("ENABLE_FRAME_CHECK", "true").lower() == "true"
ENABLE_AUDIO_CHECK = os.getenv("ENABLE_AUDIO_CHECK", "true").lower() == "true"

# تقليل عدد الإطارات ومدة الصوت للسرعة
FRAMES_TO_CHECK = int(os.getenv("FRAMES_TO_CHECK", "3"))
AUDIO_CHECK_DURATION = int(os.getenv("AUDIO_CHECK_DURATION", "60"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

user_sessions = {}


# ============= دوال مساعدة =============
def is_valid_url(text: str) -> bool:
    url_pattern = re.compile(
        r"^https?://"
        r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
        r"[A-Za-z]{2,6}"
        r"(?:/[^\s]*)?$"
    )
    return bool(url_pattern.match(text.strip()))


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def delete_file(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            logger.error(f"فشل حذف {path}: {e}")


def cleanup_session(user_id: int):
    if user_id in user_sessions:
        session = user_sessions[user_id]
        for key in ["video_path", "processed_path"]:
            delete_file(session.get(key))
        del user_sessions[user_id]


# ============= دوال التحميل =============
async def get_video_info(url: str) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract)


async def download_video(url: str, user_id: int) -> str:
    """تحميل سريع - بدون دمج الفيديو والصوت (مقبول للجودات العادية)"""
    output_template = str(DOWNLOAD_DIR / f"{user_id}_{uuid.uuid4().hex[:8]}.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        # استخدام best بدل bestvideo+bestaudio = أسرع بكثير (ما يحتاج دمج)
        # يختار أعلى جودة بملف واحد (عادة 720p)
        "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "tiktok": {"api_hostname": "api22-normal-c-alisg.tiktokv.com"},
        },
        # تسريع
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "retries": 2,
    }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if os.path.exists(filename):
                return filename
            base = os.path.splitext(filename)[0]
            for ext in [".mp4", ".mkv", ".webm"]:
                if os.path.exists(base + ext):
                    return base + ext
            return filename

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download)


async def run_ffmpeg(cmd: list) -> bool:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"خطأ ffmpeg: {stderr.decode()[:500]}")
        return False
    return True


# ============= دوال المعالجة =============
async def extract_audio(video_path: str) -> str:
    output_path = os.path.splitext(video_path)[0] + "_audio.mp3"
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", output_path]
    return output_path if await run_ffmpeg(cmd) else None


async def remove_background_music(video_path: str) -> str:
    """إزالة الموسيقى الخلفية وإبقاء الكلام"""
    output_path = os.path.splitext(video_path)[0] + "_novoice.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-af", "highpass=f=200,lowpass=f=3000,afftdn=nf=-25",
        "-c:v", "copy", "-c:a", "aac", output_path,
    ]
    return output_path if await run_ffmpeg(cmd) else None


async def remove_voice(video_path: str) -> str:
    """إزالة الكلام وإبقاء الموسيقى (عكس إزالة الموسيقى)"""
    output_path = os.path.splitext(video_path)[0] + "_music_only.mp4"
    # تقنية Center Channel Extraction: يلغي الصوت الوسطي (عادة يكون الكلام)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-af", "pan=stereo|c0=c0|c1=-1*c1,pan=mono|c0=c0+c1",
        "-c:v", "copy", "-c:a", "aac", output_path,
    ]
    return output_path if await run_ffmpeg(cmd) else None


async def trim_video(video_path: str, start: str, end: str) -> str:
    output_path = os.path.splitext(video_path)[0] + "_trimmed.mp4"
    cmd = ["ffmpeg", "-y", "-i", video_path, "-ss", start, "-to", end, "-c", "copy", output_path]
    return output_path if await run_ffmpeg(cmd) else None


async def upscale_video(video_path: str) -> str:
    output_path = os.path.splitext(video_path)[0] + "_hd.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", "scale=-2:1080:flags=lanczos",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy", output_path,
    ]
    return output_path if await run_ffmpeg(cmd) else None


async def mute_video(video_path: str) -> str:
    """إزالة الصوت كلياً"""
    output_path = os.path.splitext(video_path)[0] + "_mute.mp4"
    cmd = ["ffmpeg", "-y", "-i", video_path, "-c:v", "copy", "-an", output_path]
    return output_path if await run_ffmpeg(cmd) else None


# ============= الفلترة المتوازية (للسرعة) =============
async def run_parallel_filters(video_path: str):
    """تشغيل فحص الإطارات وفحص الكلام بالتوازي"""
    tasks = []

    if ENABLE_FRAME_CHECK:
        tasks.append(("frames", check_video_frames(video_path, num_frames=FRAMES_TO_CHECK)))
    if ENABLE_AUDIO_CHECK:
        tasks.append(("audio", _check_audio_wrapper(video_path)))

    if not tasks:
        return True, ""

    # تشغيل المهام بالتوازي
    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

    for i, result in enumerate(results):
        task_name = tasks[i][0]
        if isinstance(result, Exception):
            logger.error(f"خطأ في فحص {task_name}: {result}")
            continue
        is_safe, reason = result
        if not is_safe:
            return False, reason

    return True, ""


async def _check_audio_wrapper(video_path: str):
    """wrapper لفحص الصوت"""
    try:
        transcript = await transcribe_video(video_path, max_duration=AUDIO_CHECK_DURATION)
        if transcript:
            logger.info(f"نص: {transcript[:100]}")
            is_safe, matched = check_transcript(transcript)
            if not is_safe:
                return False, "المقطع يحتوي على كلام غير لائق"
        return True, ""
    except Exception as e:
        logger.error(f"خطأ في فحص الصوت: {e}")
        return True, ""


# ============= قائمة الخيارات =============
def get_actions_keyboard() -> InlineKeyboardMarkup:
    """لوحة أزرار العمليات"""
    keyboard = [
        [
            InlineKeyboardButton("📥 إرسال الفيديو", callback_data="send_video"),
            InlineKeyboardButton("🎧 صوت MP3", callback_data="audio"),
        ],
        [
            InlineKeyboardButton("🔇 إزالة الموسيقى", callback_data="remove_music"),
            InlineKeyboardButton("🗣 إزالة الكلام", callback_data="remove_voice"),
        ],
        [
            InlineKeyboardButton("📝 إزالة النصوص المكتوبة", callback_data="remove_text"),
        ],
        [
            InlineKeyboardButton("🔕 كتم الصوت كاملاً", callback_data="mute"),
            InlineKeyboardButton("📺 رفع الجودة", callback_data="upscale"),
        ],
        [
            InlineKeyboardButton("✂️ قص جزء", callback_data="trim"),
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_video_info(title: str, duration: int, file_size: int, source: str = "") -> str:
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "غير معروف"
    source_line = f"📡 *المصدر:* {source}\n" if source else ""
    return (
        f"✅ *جاهز للمعالجة*\n\n"
        f"📹 *العنوان:* {title[:60]}\n"
        f"{source_line}"
        f"⏱ *المدة:* {duration_str}\n"
        f"💾 *الحجم:* {format_size(file_size)}\n\n"
        f"اختر العملية التي تريدها:"
    )


# ============= معالجات الأوامر =============
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 *أهلاً بك في بوت الفيديوهات*\n\n"
        "🔹 *طريقتين للاستخدام:*\n\n"
        "1️⃣ *تحميل من رابط:*\n"
        "أرسل لي رابط من: تيك توك، يوتيوب، إنستجرام، X، فيسبوك، وأكثر.\n\n"
        "2️⃣ *معالجة فيديو من جهازك:*\n"
        "أرسل الفيديو مباشرة كملف وأنا أعالجه لك.\n\n"
        "✨ *العمليات المتاحة:*\n"
        "📥 تحميل بدون علامة مائية\n"
        "🎧 استخراج الصوت (MP3)\n"
        "🔇 إزالة الموسيقى الخلفية\n"
        "🗣 إزالة الكلام (إبقاء الموسيقى)\n"
        "📝 إزالة النصوص المكتوبة على الفيديو\n"
        "🔕 كتم الصوت كاملاً\n"
        "📺 رفع الجودة\n"
        "✂️ قص جزء من المقطع\n\n"
        "🛡 *فلتر محتوى ذكي* يرفض أي محتوى غير لائق.\n\n"
        "🚀 جرّب الآن!"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ *طريقة الاستخدام:*\n\n"
        "*📎 للتحميل من رابط:*\n"
        "1. أرسل رابط الفيديو\n"
        "2. انتظر الفحص والتحميل\n"
        "3. اختر العملية من القائمة\n\n"
        "*📹 لمعالجة فيديو من جهازك:*\n"
        "1. أرسل الفيديو كـ Video أو File\n"
        "2. اختر العملية من القائمة\n\n"
        "📌 *ملاحظات:*\n"
        "• الحد الأقصى: 50 ميجا (حد تيليجرام للبوتات)\n"
        "• الفيديوهات المرفوعة من الجهاز *لا تمر بفلتر المحتوى*\n"
        "  (لأنها فيديوهاتك الشخصية)"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# ============= معالج الروابط =============
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user_id = update.effective_user.id

    if not is_valid_url(url):
        await update.message.reply_text("❌ هذا ليس رابطاً صحيحاً.")
        return

    cleanup_session(user_id)
    status_msg = await update.message.reply_text("⏳ جاري المعالجة...")

    try:
        # الطبقة 1: فحص النطاق
        is_blocked, domain = is_blocked_domain(url)
        if is_blocked:
            await status_msg.edit_text(
                f"🚫 *تم رفض الرابط*\n\nالموقع ({domain}) محظور.",
                parse_mode="Markdown",
            )
            return

        # الطبقة 2: فحص البيانات
        await status_msg.edit_text("🔍 جاري جلب المعلومات...")
        try:
            info = await get_video_info(url)
        except Exception as e:
            await status_msg.edit_text(f"❌ فشل جلب المعلومات: {str(e)[:150]}")
            return

        is_safe, reason = check_video_metadata(info)
        if not is_safe:
            await status_msg.edit_text(
                f"🚫 *تم رفض الفيديو*\n\n{reason}",
                parse_mode="Markdown",
            )
            return

        # التحميل
        await status_msg.edit_text("⬇️ جاري التحميل...")
        try:
            video_path = await download_video(url, user_id)
        except Exception as e:
            await status_msg.edit_text(f"❌ فشل التحميل: {str(e)[:150]}")
            return

        if not video_path or not os.path.exists(video_path):
            await status_msg.edit_text("❌ فشل التحميل")
            return

        # الطبقات 3 و 4 بالتوازي
        if ENABLE_FRAME_CHECK or ENABLE_AUDIO_CHECK:
            await status_msg.edit_text("🛡 جاري فحص المحتوى...")
            is_safe, reason = await run_parallel_filters(video_path)
            if not is_safe:
                delete_file(video_path)
                await status_msg.edit_text(
                    f"🚫 *تم رفض الفيديو*\n\n{reason}",
                    parse_mode="Markdown",
                )
                return

        # ✅ العرض
        title = info.get("title", "فيديو")
        duration = info.get("duration", 0)
        uploader = info.get("uploader", "غير معروف")
        file_size = os.path.getsize(video_path)

        user_sessions[user_id] = {
            "video_path": video_path,
            "title": title,
            "duration": duration,
        }

        await status_msg.delete()
        await update.message.reply_text(
            format_video_info(title, duration, file_size, uploader),
            parse_mode="Markdown",
            reply_markup=get_actions_keyboard(),
        )

    except yt_dlp.utils.DownloadError as e:
        await status_msg.edit_text(f"❌ فشل التحميل:\n{str(e)[:200]}")
    except Exception as e:
        logger.exception("خطأ")
        await status_msg.edit_text(f"⚠️ خطأ: {str(e)[:200]}")


# ============= معالج الفيديو المرفوع من الجهاز =============
async def handle_uploaded_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الفيديو المرفوع من المستخدم مباشرة"""
    user_id = update.effective_user.id

    # استخراج الملف (فيديو أو document)
    file_obj = None
    file_name = "video.mp4"
    duration = 0

    if update.message.video:
        file_obj = update.message.video
        duration = file_obj.duration
        if file_obj.file_name:
            file_name = file_obj.file_name
    elif update.message.document:
        file_obj = update.message.document
        if file_obj.file_name:
            file_name = file_obj.file_name
        # تأكد أنه فيديو
        if not file_name.lower().endswith((".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")):
            await update.message.reply_text("❌ الملف ليس فيديو. ارفع ملف بصيغة MP4 أو MKV أو MOV.")
            return
    else:
        return

    # التحقق من الحجم
    if file_obj.file_size and file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"⚠️ الملف كبير جداً ({format_size(file_obj.file_size)}).\n"
            "الحد الأقصى: 50 ميجا (حد تيليجرام للبوتات)."
        )
        return

    cleanup_session(user_id)
    status_msg = await update.message.reply_text("⬇️ جاري استلام الفيديو...")

    try:
        # تحميل الملف
        video_path = str(DOWNLOAD_DIR / f"{user_id}_{uuid.uuid4().hex[:8]}.mp4")
        tg_file = await file_obj.get_file()
        await tg_file.download_to_drive(video_path)

        file_size = os.path.getsize(video_path)
        title = os.path.splitext(file_name)[0][:60]

        user_sessions[user_id] = {
            "video_path": video_path,
            "title": title,
            "duration": duration,
        }

        await status_msg.delete()
        await update.message.reply_text(
            format_video_info(title, duration, file_size, "فيديو من جهازك 📱"),
            parse_mode="Markdown",
            reply_markup=get_actions_keyboard(),
        )

    except Exception as e:
        logger.exception("خطأ في استلام الفيديو")
        await status_msg.edit_text(f"❌ فشل استلام الفيديو: {str(e)[:200]}")


# ============= معالج الأزرار =============
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    action = query.data

    if user_id not in user_sessions:
        await query.edit_message_text("⚠️ انتهت الجلسة. أرسل الرابط أو الفيديو من جديد.")
        return

    session = user_sessions[user_id]
    video_path = session["video_path"]

    if action == "cancel":
        cleanup_session(user_id)
        await query.edit_message_text("❌ تم الإلغاء.")
        return

    if action == "send_video":
        await query.edit_message_text("📤 جاري الإرسال...")
        await send_video_file(query, video_path, session["title"])
        cleanup_session(user_id)
        return

    if action == "audio":
        await query.edit_message_text("🎧 جاري استخراج الصوت...")
        audio_path = await extract_audio(video_path)
        if audio_path:
            session["processed_path"] = audio_path
            await send_audio_file(query, audio_path, session["title"])
        else:
            await query.edit_message_text("❌ فشل استخراج الصوت.")
        cleanup_session(user_id)
        return

    if action == "remove_music":
        await query.edit_message_text("🔇 جاري إزالة الموسيقى...")
        processed = await remove_background_music(video_path)
        if processed:
            session["processed_path"] = processed
            await send_video_file(query, processed, f"{session['title']} (بدون موسيقى)")
        else:
            await query.edit_message_text("❌ فشلت العملية.")
        cleanup_session(user_id)
        return

    if action == "remove_voice":
        await query.edit_message_text("🗣 جاري إزالة الكلام...")
        processed = await remove_voice(video_path)
        if processed:
            session["processed_path"] = processed
            await send_video_file(query, processed, f"{session['title']} (بدون كلام)")
        else:
            await query.edit_message_text("❌ فشلت العملية.")
        cleanup_session(user_id)
        return

    if action == "mute":
        await query.edit_message_text("🔕 جاري كتم الصوت...")
        processed = await mute_video(video_path)
        if processed:
            session["processed_path"] = processed
            await send_video_file(query, processed, f"{session['title']} (بدون صوت)")
        else:
            await query.edit_message_text("❌ فشلت العملية.")
        cleanup_session(user_id)
        return

    if action == "remove_text":
        await query.edit_message_text(
            "📝 جاري اكتشاف النصوص في الفيديو...\n"
            "⏳ العملية قد تستغرق 30-90 ثانية"
        )
        try:
            processed = await remove_text_from_video(video_path)
            if processed:
                session["processed_path"] = processed
                await send_video_file(query, processed, f"{session['title']} (بدون نصوص)")
            else:
                await query.edit_message_text(
                    "ℹ️ لم يتم العثور على نصوص مكتوبة في الفيديو،\n"
                    "أو فشلت عملية الإزالة."
                )
        except Exception as e:
            logger.exception("خطأ في إزالة النصوص")
            await query.edit_message_text(f"❌ فشلت العملية: {str(e)[:200]}")
        cleanup_session(user_id)
        return

    if action == "upscale":
        await query.edit_message_text("📺 جاري رفع الجودة...\n⏳ قد يستغرق دقائق")
        processed = await upscale_video(video_path)
        if processed:
            session["processed_path"] = processed
            await send_video_file(query, processed, f"{session['title']} (HD)")
        else:
            await query.edit_message_text("❌ فشلت عملية رفع الجودة.")
        cleanup_session(user_id)
        return

    if action == "trim":
        duration = session.get("duration", 0)
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "غير معروف"
        await query.edit_message_text(
            f"✂️ *قص المقطع*\n\n"
            f"مدة المقطع: {duration_str}\n\n"
            f"أرسل وقت *البداية* بصيغة `MM:SS`\n"
            f"مثال: `0:10`",
            parse_mode="Markdown",
        )
        session["awaiting"] = "trim_start"
        return


# ============= إرسال الملفات =============
async def send_video_file(query_or_update, video_path: str, caption: str):
    chat_id = (
        query_or_update.message.chat_id
        if hasattr(query_or_update, "message")
        else query_or_update.effective_chat.id
    )
    bot = query_or_update.get_bot()

    file_size = os.path.getsize(video_path)
    if file_size > MAX_FILE_SIZE:
        await bot.send_message(
            chat_id,
            f"⚠️ الحجم ({format_size(file_size)}) أكبر من حد تيليجرام (50MB).",
        )
        return

    try:
        with open(video_path, "rb") as f:
            await bot.send_video(
                chat_id=chat_id, video=f,
                caption=f"✅ {caption[:1000]}",
                supports_streaming=True,
                read_timeout=180, write_timeout=180,
            )
    except Exception as e:
        logger.exception("فشل إرسال الفيديو")
        await bot.send_message(chat_id, f"❌ فشل الإرسال: {str(e)[:200]}")


async def send_audio_file(query_or_update, audio_path: str, title: str):
    chat_id = (
        query_or_update.message.chat_id
        if hasattr(query_or_update, "message")
        else query_or_update.effective_chat.id
    )
    bot = query_or_update.get_bot()

    file_size = os.path.getsize(audio_path)
    if file_size > MAX_FILE_SIZE:
        await bot.send_message(chat_id, "⚠️ الملف أكبر من حد تيليجرام.")
        return

    try:
        with open(audio_path, "rb") as f:
            await bot.send_audio(
                chat_id=chat_id, audio=f,
                title=title[:60],
                read_timeout=180, write_timeout=180,
            )
    except Exception as e:
        logger.exception("فشل إرسال الصوت")
        await bot.send_message(chat_id, f"❌ فشل الإرسال: {str(e)[:200]}")


async def handle_trim_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        return

    session = user_sessions[user_id]
    awaiting = session.get("awaiting")

    if awaiting not in ("trim_start", "trim_end"):
        return

    text = update.message.text.strip()

    if not re.match(r"^\d{1,2}(:\d{2}){1,2}$", text):
        await update.message.reply_text(
            "❌ صيغة الوقت غير صحيحة. استخدم `MM:SS`",
            parse_mode="Markdown",
        )
        return

    if awaiting == "trim_start":
        session["trim_start"] = text
        session["awaiting"] = "trim_end"
        await update.message.reply_text(
            f"✅ البداية: `{text}`\n\nأرسل وقت *النهاية*:",
            parse_mode="Markdown",
        )
        return

    if awaiting == "trim_end":
        session["trim_end"] = text
        session["awaiting"] = None

        status_msg = await update.message.reply_text("✂️ جاري قص المقطع...")
        try:
            processed = await trim_video(
                session["video_path"],
                session["trim_start"],
                session["trim_end"],
            )
            if processed:
                session["processed_path"] = processed
                await status_msg.delete()
                await send_video_file(update, processed, f"{session['title']} (مقصوص)")
            else:
                await status_msg.edit_text("❌ فشل قص المقطع.")
        finally:
            cleanup_session(user_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message.text else ""

    if user_id in user_sessions and user_sessions[user_id].get("awaiting"):
        await handle_trim_input(update, context)
        return

    if is_valid_url(text):
        await handle_url(update, context)
        return

    await update.message.reply_text(
        "📎 أرسل رابط فيديو أو ارفع فيديو من جهازك للبدء.\n"
        "اكتب /help للمزيد."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"خطأ: {context.error}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    # معالج الفيديوهات المرفوعة
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_uploaded_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🚀 البوت يعمل الآن...")
    logger.info(f"فحص الإطارات: {'مفعّل' if ENABLE_FRAME_CHECK else 'معطّل'} ({FRAMES_TO_CHECK} إطار)")
    logger.info(f"فحص الكلام: {'مفعّل' if ENABLE_AUDIO_CHECK else 'معطّل'} ({AUDIO_CHECK_DURATION}s)")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
