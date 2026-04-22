"""
وحدة تحويل الصوت إلى نص
تستخدم faster-whisper - نموذج Whisper مُحسّن للسرعة والذاكرة
يدعم العربية والإنجليزية بشكل ممتاز
"""

import os
import asyncio
import logging
import tempfile

logger = logging.getLogger(__name__)

# تحميل النموذج مرة واحدة
_model = None


def get_model():
    """تحميل نموذج Whisper مرة واحدة"""
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
            # استخدام نموذج tiny - الأخف والأسرع (~75 MB)
            # int8 للتوافق مع CPU وتوفير الذاكرة
            _model = WhisperModel(
                "tiny",
                device="cpu",
                compute_type="int8",
                download_root="./models",
            )
            logger.info("✅ تم تحميل نموذج Whisper")
        except ImportError:
            logger.warning("⚠️ مكتبة faster-whisper غير مثبتة - سيتم تخطي فحص الصوت")
            return None
        except Exception as e:
            logger.error(f"فشل تحميل Whisper: {e}")
            return None
    return _model


async def extract_audio_for_transcription(video_path: str) -> str:
    """استخراج الصوت من الفيديو بصيغة مناسبة للتحويل"""
    audio_path = tempfile.mktemp(suffix=".wav")

    # تحويل إلى WAV 16kHz mono (الأنسب لـ Whisper)
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        audio_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()

    if process.returncode != 0 or not os.path.exists(audio_path):
        return None
    return audio_path


async def transcribe_video(video_path: str, max_duration: int = 180) -> str:
    """
    تحويل صوت الفيديو إلى نص
    max_duration: أقصى مدة بالثواني للتحويل (لتوفير الوقت في المقاطع الطويلة)
    Returns: النص المستخرج أو سلسلة فارغة
    """
    model = get_model()
    if model is None:
        return ""

    audio_path = None
    try:
        audio_path = await extract_audio_for_transcription(video_path)
        if not audio_path:
            logger.warning("فشل استخراج الصوت")
            return ""

        def _transcribe():
            """تشغيل Whisper في thread منفصل"""
            try:
                segments, info = model.transcribe(
                    audio_path,
                    beam_size=1,  # أسرع
                    vad_filter=True,  # تجاهل الصمت
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                # تجميع النص، مع حد أقصى للمدة
                text_parts = []
                for segment in segments:
                    if segment.start > max_duration:
                        break
                    text_parts.append(segment.text)
                return " ".join(text_parts).strip()
            except Exception as e:
                logger.error(f"خطأ في Whisper: {e}")
                return ""

        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _transcribe)
        return text

    except Exception as e:
        logger.exception("خطأ في تحويل الصوت")
        return ""

    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass
