"""
وحدة فحص الإطارات للكشف عن المحتوى العاري
تستخدم NudeNet - نموذج AI خفيف لاكتشاف العري
"""

import os
import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# تحميل النموذج مرة واحدة (lazy loading)
_detector = None


def get_detector():
    """تحميل نموذج NudeNet مرة واحدة"""
    global _detector
    if _detector is None:
        try:
            from nudenet import NudeDetector
            _detector = NudeDetector()
            logger.info("✅ تم تحميل نموذج NudeNet")
        except ImportError:
            logger.warning("⚠️ مكتبة nudenet غير مثبتة - سيتم تخطي فحص الصور")
            return None
        except Exception as e:
            logger.error(f"فشل تحميل NudeNet: {e}")
            return None
    return _detector


# التصنيفات التي تُعتبر محتوى غير لائق
UNSAFE_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
}

# الحد الأدنى للثقة لاعتبار الكشف صحيحاً
CONFIDENCE_THRESHOLD = 0.5


async def extract_frames(video_path: str, num_frames: int = 5) -> list[str]:
    """
    استخراج عدد من الإطارات من الفيديو بشكل متوزع
    Returns: قائمة بمسارات الإطارات المستخرجة
    """
    frames_dir = tempfile.mkdtemp(prefix="frames_")
    output_pattern = os.path.join(frames_dir, "frame_%03d.jpg")

    # استخراج الإطارات بشكل متوزع
    # fps=1/x يعني إطار كل x ثانية، لكن أسهل نستخدم select
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"select='not(mod(n\\,30))',scale=320:-1",
        "-vsync", "vfr",
        "-frames:v", str(num_frames),
        "-q:v", "3",
        output_pattern,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()

    # جمع الإطارات المستخرجة
    frames = sorted(Path(frames_dir).glob("frame_*.jpg"))
    return [str(f) for f in frames]


async def check_video_frames(video_path: str, num_frames: int = 5) -> tuple[bool, str]:
    """
    فحص إطارات الفيديو للكشف عن المحتوى العاري
    Returns: (is_safe, reason_if_blocked)
    """
    detector = get_detector()
    if detector is None:
        # لو النموذج ما تحمّل، نعتبر آمن (لا نعطل البوت)
        return True, ""

    frames = []
    try:
        frames = await extract_frames(video_path, num_frames)
        if not frames:
            logger.warning("لم يتم استخراج أي إطارات")
            return True, ""

        def _detect_all():
            """فحص كل الإطارات في thread منفصل"""
            unsafe_count = 0
            details = []
            for frame_path in frames:
                try:
                    detections = detector.detect(frame_path)
                    for det in detections:
                        class_name = det.get("class", "")
                        score = det.get("score", 0)
                        if class_name in UNSAFE_CLASSES and score >= CONFIDENCE_THRESHOLD:
                            unsafe_count += 1
                            details.append(f"{class_name} ({score:.2f})")
                            break  # إطار واحد سيء كفاية
                except Exception as e:
                    logger.error(f"خطأ في فحص الإطار {frame_path}: {e}")
            return unsafe_count, details

        loop = asyncio.get_event_loop()
        unsafe_count, details = await loop.run_in_executor(None, _detect_all)

        # إذا في إطار واحد على الأقل فيه محتوى غير لائق، نرفض
        if unsafe_count > 0:
            logger.info(f"تم رفض الفيديو - إطارات غير آمنة: {details}")
            return False, "تم اكتشاف محتوى غير لائق في الفيديو"

        return True, ""

    except Exception as e:
        logger.exception("خطأ في فحص الإطارات")
        return True, ""  # نسمح بالفيديو لو صار خطأ تقني

    finally:
        # تنظيف الإطارات
        for frame in frames:
            try:
                os.remove(frame)
            except Exception:
                pass
        # حذف المجلد المؤقت
        try:
            parent = Path(frames[0]).parent if frames else None
            if parent and parent.exists():
                parent.rmdir()
        except Exception:
            pass
