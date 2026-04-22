"""
وحدة إزالة النصوص المكتوبة على الفيديو
تستخدم EasyOCR لاكتشاف النصوص + OpenCV inpainting لإخفائها
"""

import os
import asyncio
import logging
import tempfile

logger = logging.getLogger(__name__)

_reader = None


def get_reader():
    """تحميل نموذج EasyOCR مرة واحدة (يدعم عربي وإنجليزي)"""
    global _reader
    if _reader is None:
        try:
            import easyocr
            # ar = عربي، en = إنجليزي
            # gpu=False عشان CPU
            _reader = easyocr.Reader(["ar", "en"], gpu=False, verbose=False)
            logger.info("✅ تم تحميل EasyOCR")
        except ImportError:
            logger.warning("⚠️ easyocr غير مثبت")
            return None
        except Exception as e:
            logger.error(f"فشل تحميل EasyOCR: {e}")
            return None
    return _reader


async def get_video_info(video_path: str) -> dict:
    """الحصول على معلومات الفيديو (العرض، الطول، المدة)"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        video_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()

    import json
    try:
        data = json.loads(stdout.decode())
        video_stream = next(
            (s for s in data["streams"] if s["codec_type"] == "video"), None
        )
        if video_stream:
            return {
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "duration": float(data["format"].get("duration", 0)),
            }
    except Exception as e:
        logger.error(f"فشل قراءة معلومات الفيديو: {e}")
    return {}


async def detect_text_regions(video_path: str, num_samples: int = 5) -> list:
    """
    اكتشاف مناطق النصوص في الفيديو عبر أخذ عينات من الإطارات
    Returns: قائمة بالمناطق [(x1, y1, x2, y2), ...] موحّدة
    """
    reader = get_reader()
    if reader is None:
        return []

    # استخراج عينات من الإطارات
    frames_dir = tempfile.mkdtemp(prefix="ocr_frames_")
    output_pattern = os.path.join(frames_dir, "frame_%03d.jpg")

    info = await get_video_info(video_path)
    duration = info.get("duration", 0)

    if duration < 1:
        return []

    # نأخذ عينات موزعة على طول الفيديو
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"fps=1/{max(1, int(duration / num_samples))}",
        "-frames:v", str(num_samples),
        "-q:v", "3",
        output_pattern,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()

    from pathlib import Path
    frames = sorted(Path(frames_dir).glob("frame_*.jpg"))
    if not frames:
        return []

    def _detect_all():
        """اكتشاف النصوص في كل إطار (في thread منفصل)"""
        all_regions = []
        for frame_path in frames:
            try:
                results = reader.readtext(str(frame_path), detail=1, paragraph=False)
                for (bbox, text, confidence) in results:
                    if confidence < 0.3 or len(text.strip()) < 2:
                        continue
                    # bbox = [[x1,y1], [x2,y1], [x2,y2], [x1,y2]]
                    xs = [p[0] for p in bbox]
                    ys = [p[1] for p in bbox]
                    x1, y1 = int(min(xs)), int(min(ys))
                    x2, y2 = int(max(xs)), int(max(ys))
                    all_regions.append((x1, y1, x2, y2, text))
            except Exception as e:
                logger.error(f"خطأ OCR في {frame_path}: {e}")
        return all_regions

    loop = asyncio.get_event_loop()
    regions = await loop.run_in_executor(None, _detect_all)

    # تنظيف الإطارات
    for frame in frames:
        try:
            os.remove(frame)
        except Exception:
            pass
    try:
        os.rmdir(frames_dir)
    except Exception:
        pass

    logger.info(f"تم اكتشاف {len(regions)} منطقة نص")
    return regions


def merge_overlapping_regions(regions: list, padding: int = 5) -> list:
    """دمج المناطق المتداخلة/المتقاربة"""
    if not regions:
        return []

    # تحويل لقائمة بدون النص
    boxes = [(r[0], r[1], r[2], r[3]) for r in regions]

    # توسيع كل منطقة بقليل
    boxes = [
        (x1 - padding, y1 - padding, x2 + padding, y2 + padding)
        for x1, y1, x2, y2 in boxes
    ]

    # دمج المناطق المتداخلة
    merged = []
    used = [False] * len(boxes)

    for i in range(len(boxes)):
        if used[i]:
            continue
        x1, y1, x2, y2 = boxes[i]
        used[i] = True

        changed = True
        while changed:
            changed = False
            for j in range(len(boxes)):
                if used[j]:
                    continue
                bx1, by1, bx2, by2 = boxes[j]
                # فحص التداخل
                if not (bx1 > x2 or bx2 < x1 or by1 > y2 or by2 < y1):
                    x1 = min(x1, bx1)
                    y1 = min(y1, by1)
                    x2 = max(x2, bx2)
                    y2 = max(y2, by2)
                    used[j] = True
                    changed = True

        merged.append((max(0, x1), max(0, y1), x2, y2))

    return merged


async def remove_text_from_video(video_path: str) -> str:
    """
    إزالة النصوص المكتوبة من الفيديو
    يستخدم طريقة delogo فلتر من ffmpeg (سريع وفعّال)
    """
    # اكتشاف مناطق النصوص
    regions = await detect_text_regions(video_path, num_samples=5)

    if not regions:
        logger.warning("لم يتم اكتشاف أي نصوص في الفيديو")
        return None

    # دمج المناطق المتداخلة
    merged_regions = merge_overlapping_regions(regions, padding=8)
    logger.info(f"بعد الدمج: {len(merged_regions)} منطقة")

    if not merged_regions:
        return None

    # الحصول على أبعاد الفيديو
    info = await get_video_info(video_path)
    video_width = info.get("width", 0)
    video_height = info.get("height", 0)

    # بناء سلسلة فلاتر delogo لكل منطقة
    # delogo=x=X:y=Y:w=W:h=H
    filters = []
    for x1, y1, x2, y2 in merged_regions:
        w = max(4, x2 - x1)
        h = max(4, y2 - y1)
        # التأكد من عدم تجاوز حدود الفيديو
        x1 = max(0, min(x1, video_width - w))
        y1 = max(0, min(y1, video_height - h))
        filters.append(f"delogo=x={x1}:y={y1}:w={w}:h={h}")

    filter_chain = ",".join(filters)

    output_path = os.path.splitext(video_path)[0] + "_notext.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", filter_chain,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "copy",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if process.returncode != 0:
        logger.error(f"فشل ffmpeg delogo: {stderr.decode()[:300]}")
        return None

    if os.path.exists(output_path):
        return output_path
    return None
