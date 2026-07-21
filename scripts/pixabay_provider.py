"""
pixabay_provider.py
مهمة هذا الملف فقط: التعامل مع Pixabay (صور + فيديوهات) — لا شيء غيره.
تم فصله من asset_fetcher.py (الذي أصبح الآن "موزّع" بين هذا الملف
و pexels_provider.py) لتسهيل الصيانة مستقبلاً.

نفس المنطق الأصلي بالضبط بدون أي تغيير بالسلوك:
- fetch_images(): صور ثابتة (image_type=photo)
- fetch_videos(): فيديوهات (video_type=film) مع درجة تطابق مع الكلمة
  المفتاحية (relevance_score) لأن Pixabay أحياناً يرجع نتائج بعيدة عن
  الموضوع المطلوب.
"""
import re

import requests
from requests.utils import quote
from scripts import config

MIN_WIDTH = 1080  # مخفّض من 1920 لأن أغلب صور Pixabay العمودية أضيق من هذا


def _re_split_words(text: str) -> list[str]:
    """تقسيم بسيط لكلمات نص (يتجاهل الفواصل وعلامات الترقيم)."""
    return [w for w in re.split(r'[^a-zA-Z0-9]+', text) if w]


def relevance_score(keyword: str, tags: str) -> float:
    """
    تقيس نسبة تداخل كلمات الكلمة المفتاحية مع وسوم Pixabay الراجعة (tags
    نص مفصول بفواصل مثل "matrix, code, green, technology"). ترجع نسبة من
    0 إلى 1: عدد كلمات الكلمة المفتاحية الموجودة فعلياً ضمن الوسوم مقسومة
    على عدد كلمات الكلمة المفتاحية الكلي.

    المطابقة تشمل تطابق البادئة (startswith) بطول 4 أحرف فأكثر، لالتقاط
    اختلافات الجذر اللغوي الشائعة (Japanese/Japan, walking/walk).
    """
    kw_words = [w.lower() for w in _re_split_words(keyword)]
    if not kw_words:
        return 0.0
    tag_words = [w.lower() for w in _re_split_words(tags)]
    if not tag_words:
        return 0.0

    def word_matches(kw_word: str) -> bool:
        for tw in tag_words:
            if kw_word == tw:
                return True
            if len(kw_word) >= 4 and len(tw) >= 4 and (kw_word.startswith(tw) or tw.startswith(kw_word)):
                return True
        return False

    matched = sum(1 for w in kw_words if word_matches(w))
    return matched / len(kw_words)


def fetch_images(keyword: str, per_page: int = 3, orientation: str = "horizontal") -> list[str]:
    if not config.PIXABAY_API_KEY:
        return []

    # إصلاح الخطأ 400: واجهة Pixabay ترفض أي قيمة أقل من 3
    api_per_page = max(3, per_page)

    encoded_keyword = quote(keyword)
    url = (
        f"https://pixabay.com/api/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}"
        f"&image_type=photo&orientation={orientation}&per_page={api_per_page}&safesearch=true"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        return [h["largeImageURL"] for h in hits]
    except Exception as e:
        print(f"[PIXABAY ERROR] فشل جلب صور Pixabay لـ '{keyword}' (orientation={orientation}): {e}")
        return []


def fetch_videos(keyword: str, per_page: int = 3) -> list[dict]:
    """
    يرجع قائمة dicts فيها {"url", "width", "height", "score"} مرتّبة من
    الأعلى تطابقاً مع الكلمة المفتاحية للأقل.

    ملاحظة مهمة: لا نفلتر حسب الاتجاه (عمودي/أفقي) لأن:
    - 95%+ من فيديوهات Pixabay أفقية
    - Remotion يتعامل مع القص تلقائياً عبر objectFit:'cover'
    - حذف الفلتر يعني نتائج فيديو أكثر بكثير بدل السقوط للصور الثابتة
    """
    if not config.PIXABAY_API_KEY:
        return []

    # نطلب أكثر من المطلوب فعلياً (حد أقصى 10) لإعطاء خوارزمية التطابق
    # مرشحين كفاية للاختيار بينهم بدل الاكتفاء بأول 3 نتائج فقط
    api_per_page = max(3, min(10, per_page * 4))

    encoded_keyword = quote(keyword)
    url = (
        f"https://pixabay.com/api/videos/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}"
        f"&video_type=film&per_page={api_per_page}&safesearch=true"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        results = []
        for h in hits:
            videos = h.get("videos", {})
            chosen = videos.get("medium") or videos.get("small") or videos.get("large")
            if not chosen or not chosen.get("url"):
                continue
            width, height = chosen.get("width", 0), chosen.get("height", 0)
            score = relevance_score(keyword, h.get("tags", ""))
            results.append({"url": chosen["url"], "width": width, "height": height, "score": score})
        # الأعلى تطابقاً أولاً
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
    except Exception as e:
        print(f"[PIXABAY ERROR] فشل جلب فيديوهات Pixabay لـ '{keyword}': {e}")
        return []
