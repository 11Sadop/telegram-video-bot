"""
وحدة فلترة المحتوى
تحتوي على:
- قائمة النطاقات الإباحية الممنوعة
- قائمة الكلمات المفتاحية النابية (عربي/إنجليزي)
- دوال فحص النص
"""

import re
from urllib.parse import urlparse


# ============= النطاقات الإباحية الممنوعة =============
# قائمة بأشهر المواقع الإباحية - يتم رفض أي رابط منها مباشرة
BLOCKED_DOMAINS = {
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
    "redtube.com", "youporn.com", "tube8.com", "spankbang.com",
    "porn.com", "beeg.com", "tnaflix.com", "drtuber.com",
    "sunporno.com", "nuvid.com", "porntrex.com", "eporner.com",
    "hclips.com", "txxx.com", "upornia.com", "gotporn.com",
    "fapvid.com", "hqporner.com", "hdporn.com", "pornone.com",
    "youjizz.com", "porndig.com", "4tube.com", "empflix.com",
    "slutload.com", "keezmovies.com", "pornhd.com", "porndoe.com",
    "brazzers.com", "bangbros.com", "realitykings.com", "naughtyamerica.com",
    "mofos.com", "digitalplayground.com", "vivid.com", "wicked.com",
    "chaturbate.com", "livejasmin.com", "stripchat.com", "bongacams.com",
    "cam4.com", "myfreecams.com", "camsoda.com", "flirt4free.com",
    "onlyfans.com", "fansly.com", "manyvids.com", "clips4sale.com",
    "adultfriendfinder.com", "ashleymadison.com", "fetlife.com",
    "thumbzilla.com", "pornhat.com", "pornrabbit.com", "fuq.com",
    "hotmovs.com", "iceporn.com", "definebabe.com", "orgasm.com",
    "xbabe.com", "freepornvs.com", "pornoeggs.com",
    # مواقع عربية
    "sex-egypt.net", "arabsexyt.com", "arab-sex.org",
}


# ============= الكلمات المفتاحية الإباحية/النابية =============
# عربية
ADULT_KEYWORDS_AR = [
    # محتوى جنسي صريح
    "جنس", "سكس", "اباحي", "إباحي", "اباحية", "إباحية", "بورن",
    "عاري", "عارية", "عاريات", "عراة", "تعرية", "مثير", "مثيرة",
    "نيك", "نايك", "ينيك", "منيوك", "كس", "زب", "قضيب", "مهبل",
    "شرموط", "شرموطة", "شراميط", "عاهرة", "عاهرات", "قحبة", "قحاب",
    "مص", "مصمص", "لحس", "متناكة", "متناك", "اغتصاب", "اغتصب",
    "18+", "للكبار فقط", "ممنوع للأطفال",
    # شتائم وإهانات
    "كلب", "كلاب", "ابن كلب", "ابن الكلب", "ابن حرام", "ابن الحرام",
    "ابن عاهرة", "ولد حرام", "ولد عاهرة", "حقير", "وسخ", "قذر",
]

# إنجليزية
ADULT_KEYWORDS_EN = [
    # sexual content
    "porn", "pornography", "xxx", "nsfw", "nude", "nudes", "naked",
    "sex", "sexy", "sexual", "erotic", "erotica", "hardcore", "softcore",
    "fuck", "fucking", "fucked", "fucker", "motherfucker", "mf",
    "pussy", "dick", "cock", "penis", "vagina", "boobs", "tits", "ass",
    "asshole", "bitch", "whore", "slut", "cum", "cumshot", "blowjob",
    "handjob", "anal", "orgasm", "masturbat", "horny", "milf",
    "onlyfans", "camgirl", "webcam sex", "adult content", "18+",
    "escort", "hooker", "prostitut",
    # slurs and curses
    "shit", "bullshit", "bastard", "damn",
]

# تحويل لـ set للبحث السريع
ADULT_KEYWORDS_ALL = set(
    [k.lower() for k in ADULT_KEYWORDS_AR] +
    [k.lower() for k in ADULT_KEYWORDS_EN]
)

BLOCKED_DOMAINS_LOWER = {d.lower() for d in BLOCKED_DOMAINS}


# ============= دوال الفحص =============
def is_blocked_domain(url: str) -> tuple[bool, str]:
    """
    فحص إذا كان الرابط من موقع ممنوع
    Returns: (is_blocked, domain)
    """
    try:
        parsed = urlparse(url.lower())
        domain = parsed.netloc.replace("www.", "")

        # فحص مباشر
        if domain in BLOCKED_DOMAINS_LOWER:
            return True, domain

        # فحص النطاقات الفرعية (subdomains)
        for blocked in BLOCKED_DOMAINS_LOWER:
            if domain.endswith("." + blocked) or domain == blocked:
                return True, blocked

        return False, domain
    except Exception:
        return False, ""


def contains_adult_keywords(text: str) -> tuple[bool, list[str]]:
    """
    فحص النص إذا كان يحتوي على كلمات مفتاحية إباحية/نابية
    Returns: (has_adult_content, matched_keywords)
    """
    if not text:
        return False, []

    text_lower = text.lower()
    # إزالة علامات الترقيم لتحسين المطابقة
    normalized = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text_lower)

    matched = []
    for keyword in ADULT_KEYWORDS_ALL:
        # استخدام حدود الكلمات للعربية والإنجليزية
        # نبحث عن الكلمة كاملة أو كجزء من كلمة للكلمات العربية القصيرة
        if len(keyword) <= 3:
            # للكلمات القصيرة نستخدم حدود الكلمات الصارمة
            pattern = r"(?:^|\s)" + re.escape(keyword) + r"(?:\s|$)"
            if re.search(pattern, normalized):
                matched.append(keyword)
        else:
            if keyword in normalized:
                matched.append(keyword)

    return len(matched) > 0, matched


def check_video_metadata(info: dict) -> tuple[bool, str]:
    """
    فحص بيانات الفيديو (العنوان، الوصف، التصنيفات)
    Returns: (is_safe, reason_if_blocked)
    """
    # 1. فحص تصنيف NSFW من المنصة نفسها
    if info.get("age_limit", 0) >= 18:
        return False, "الفيديو مصنّف 18+ من المنصة نفسها"

    # 2. فحص العنوان
    title = info.get("title", "")
    has_bad, matches = contains_adult_keywords(title)
    if has_bad:
        return False, f"العنوان يحتوي على كلمات غير لائقة"

    # 3. فحص الوصف
    description = info.get("description", "")
    if description:
        # نأخذ أول 500 حرف فقط للسرعة
        desc_sample = description[:500]
        has_bad, matches = contains_adult_keywords(desc_sample)
        if has_bad:
            return False, f"وصف الفيديو يحتوي على كلمات غير لائقة"

    # 4. فحص التصنيفات/الوسوم
    tags = info.get("tags", []) or []
    categories = info.get("categories", []) or []
    combined = " ".join([str(t) for t in tags] + [str(c) for c in categories])
    if combined:
        has_bad, matches = contains_adult_keywords(combined)
        if has_bad:
            return False, f"تصنيفات الفيديو تحتوي على محتوى غير مناسب"

    # 5. فحص اسم القناة/الناشر
    uploader = info.get("uploader", "") or info.get("channel", "")
    has_bad, matches = contains_adult_keywords(uploader)
    if has_bad:
        return False, f"قناة الناشر غير مناسبة"

    return True, ""


def check_transcript(text: str) -> tuple[bool, list[str]]:
    """
    فحص النص المستخرج من الصوت (transcription)
    Returns: (is_safe, matched_bad_words)
    """
    has_bad, matched = contains_adult_keywords(text)
    return not has_bad, matched
