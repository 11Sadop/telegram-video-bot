"""
بوت تحميل الفيديوهات - تيليجرام (مع فلترة المحتوى)
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

# ============= الإعدادات =============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("⚠️ لم يتم العثور على BOT_TOKEN في متغيرات البيئة")

ENABLE_FRAME_CHECK = os.getenv("ENABLE_FRAME_CHECK", "true").lower() == "true"
ENABLE_AUDIO_CHECK = os.getenv("ENABLE_AUDIO_CHECK", "true").lower() == "true"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 50 * 1024 * 1024

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
    output_template = str(DOWNLOAD_DIR / f"{user_id}_{uuid.uuid4().hex[:8]}.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "extractor_args": {
            "tiktok": {"api_hostname": "api22-normal-c-alisg.tiktokv.com"},
        },
    }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
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


async def extract_audio(video_path: str) -> str:
    output_path = os.path.splitext(video_path)[0] + "_audio.mp3"
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", output_path]
    return output_path if await run_ffmpeg(cmd) else None


async def remove_background_music(video_path: str) -> str:
    output_path = os.path.splitext(video_path)[0] + "_novoice.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-af", "highpass=f=200,lowpass=f=3000,afftdn=nf=-25",
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
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy", output_path,
    ]
    return output_path if await run_ffmpeg(cmd) else None


# ============= نظام الفلترة الشامل =============
async def run_content_filters(url: str, user_id: int, status_msg):
    """
    تشغيل كل طبقات الفلترة
    Returns: (is_safe, reason, info, video_path)
    """
    # الطبقة 1: فحص النطاق
    is_blocked, domain = is_blocked_domain(url)
    if is_blocked:
        return False, f"هذا الموقع ({domain}) محظور - محتوى غير مسموح", {}, None

    # الطبقة 2: فحص البيانات الوصفية
    await status_msg.edit_text("🔍 جاري فحص معلومات الفيديو...")
    try:
        info = await get_video_info(url)
    except Exception as e:
        return False, f"فشل جلب معلومات الفيديو: {str(e)[:150]}", {}, None

    is_safe, reason = check_video_metadata(info)
    if not is_safe:
        return False, reason, info, None

    # التحميل
    await status_msg.edit_text("⬇️ جاري تحميل الفيديو للفحص...")
    try:
        video_path = await download_video(url, user_id)
    except Exception as e:
        return False, f"فشل التحميل: {str(e)[:150]}", info, None

    # الطبقة 3: فحص الإطارات
    if ENABLE_FRAME_CHECK:
        await status_msg.edit_text("🖼 جاري فحص إطارات الفيديو...")
        try:
            is_safe, reason = await check_video_frames(video_path, num_frames=5)
            if not is_safe:
                delete_file(video_path)
                return False, reason, info, None
        except Exception as e:
            logger.error(f"خطأ في فحص الإطارات: {e}")

    # الطبقة 4: فحص الكلام
    if ENABLE_AUDIO_CHECK:
        await status_msg.edit_text("🎤 جاري فحص الكلام في المقطع...")
        try:
            transcript = await transcribe_video(video_path, max_duration=180)
            if transcript:
                logger.info(f"نص مستخرج: {transcript[:200]}")
                is_safe, matched = check_transcript(transcript)
                if not is_safe:
                    delete_file(video_path)
                    return False, "المقطع يحتوي على كلام غير لائق", info, None
        except Exception as e:
            logger.error(f"خطأ في فحص الكلام: {e}")

    return True, "", info, video_path


# ============= معالجات الأوامر =============
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 *أهلاً بك في بوت تحميل الفيديوهات*\n\n"
        "📥 أرسل لي رابط أي فيديو من:\n"
        "• تيك توك (TikTok)\n"
        "• إنستجرام (Instagram)\n"
        "• يوتيوب (YouTube)\n"
        "• تويتر/X\n"
        "• فيسبوك (Facebook)\n"
        "• وأكثر من 1000 منصة!\n\n"
        "✨ *الميزات:*\n"
        "✅ تحميل بدون علامة مائية\n"
        "🎵 إزالة الموسيقى\n"
        "🎧 استخراج الصوت (MP3)\n"
        "📺 رفع الجودة\n"
        "✂️ قص جزء من المقطع\n\n"
        "🛡 *فلتر محتوى ذكي:*\n"
        "البوت يرفض تحميل أي مقطع يحتوي على:\n"
        "• محتوى إباحي أو عُري\n"
        "• كلام نابي أو غير لائق\n\n"
        "🚀 أرسل الرابط الآن للبدء!"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ *طريقة الاستخدام:*\n\n"
        "1️⃣ أرسل رابط الفيديو\n"
        "2️⃣ البوت يفحص المحتوى أولاً تلقائياً\n"
        "3️⃣ إذا كان آمناً، اختر العملية اللي تبيها\n"
        "4️⃣ انتظر المعالجة والإرسال\n\n"
        "📌 *ملاحظات:*\n"
        "• الحد الأقصى للملف: 50 ميجا\n"
        "• الفحص قد يستغرق 30-60 ثانية\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user_id = update.effective_user.id

    if not is_valid_url(url):
        await update.message.reply_text("❌ هذا ليس رابطاً صحيحاً.")
        return

    cleanup_session(user_id)
    status_msg = await update.message.reply_text("⏳ جاري المعالجة...")

    try:
        is_safe, reason, info, video_path = await run_content_filters(
            url, user_id, status_msg
        )

        if not is_safe:
            await status_msg.edit_text(
                f"🚫 *تم رفض الفيديو*\n\n"
                f"السبب: {reason}\n\n"
                f"هذا البوت يرفض أي محتوى غير لائق.",
                parse_mode="Markdown",
            )
            return

        title = info.get("title", "فيديو")
        duration = info.get("duration", 0)
        uploader = info.get("uploader", "غير معروف")
        file_size = os.path.getsize(video_path)

        user_sessions[user_id] = {
            "url": url,
            "video_path": video_path,
            "title": title,
            "duration": duration,
        }

        keyboard = [
            [
                InlineKeyboardButton("📥 إرسال الفيديو", callback_data="send_video"),
                InlineKeyboardButton("🎧 صوت MP3", callback_data="audio"),
            ],
            [
                InlineKeyboardButton("🔇 إزالة الموسيقى", callback_data="remove_music"),
                InlineKeyboardButton("📺 رفع الجودة", callback_data="upscale"),
            ],
            [InlineKeyboardButton("✂️ قص جزء", callback_data="trim")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
        ]

        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "غير معروف"
        caption = (
            f"✅ *تم الفحص والتحميل بنجاح*\n\n"
            f"📹 *العنوان:* {title[:60]}\n"
            f"👤 *الناشر:* {uploader}\n"
            f"⏱ *المدة:* {duration_str}\n"
            f"💾 *الحجم:* {format_size(file_size)}\n\n"
            f"اختر العملية التي تريدها:"
        )

        await status_msg.delete()
        await update.message.reply_text(
            caption,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except yt_dlp.utils.DownloadError as e:
        await status_msg.edit_text(
            f"❌ فشل تحميل الفيديو:\n{str(e)[:200]}"
        )
    except Exception as e:
        logger.exception("خطأ في معالجة الرابط")
        await status_msg.edit_text(f"⚠️ حدث خطأ: {str(e)[:200]}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    action = query.data

    if user_id not in user_sessions:
        await query.edit_message_text("⚠️ انتهت الجلسة. أرسل الرابط مرة أخرى.")
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
        if audio_path and os.path.exists(audio_path):
            session["processed_path"] = audio_path
            await send_audio_file(query, audio_path, session["title"])
        else:
            await query.edit_message_text("❌ فشل استخراج الصوت.")
        cleanup_session(user_id)
        return

    if action == "remove_music":
        await query.edit_message_text("🔇 جاري إزالة الموسيقى...")
        processed = await remove_background_music(video_path)
        if processed and os.path.exists(processed):
            session["processed_path"] = processed
            await send_video_file(query, processed, f"{session['title']} (بدون موسيقى)")
        else:
            await query.edit_message_text("❌ فشلت العملية.")
        cleanup_session(user_id)
        return

    if action == "upscale":
        await query.edit_message_text("📺 جاري رفع الجودة...\n⏳ قد يستغرق دقائق")
        processed = await upscale_video(video_path)
        if processed and os.path.exists(processed):
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
                read_timeout=120, write_timeout=120,
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
                read_timeout=120, write_timeout=120,
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
            if processed and os.path.exists(processed):
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
        "📎 أرسل لي رابط فيديو للبدء.\nاستخدم /help لمعرفة المزيد."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"خطأ: {context.error}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🚀 البوت يعمل الآن...")
    logger.info(f"فحص الإطارات: {'مفعّل' if ENABLE_FRAME_CHECK else 'معطّل'}")
    logger.info(f"فحص الكلام: {'مفعّل' if ENABLE_AUDIO_CHECK else 'معطّل'}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
