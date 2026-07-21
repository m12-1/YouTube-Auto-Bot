"""
pexels_provider.py
مهمة هذا الملف فقط: التعامل مع Pexels (صور + فيديوهات) عبر PEXELS_API_KEY.

سابقاً كان مفتاح Pexels موجوداً بالكود لكن غير مُفعّل فعلياً بمسار جلب
الوسائط (get_media_for_scene بـ asset_fetcher.py كان يستخدم Pixabay فقط).
هذا الملف يجعل Pexels يعمل بالكامل (صور + فيديوهات)، ثم asset_fetcher.py
يوزّع العمل بين هذا الملف و pixabay_provider.py.

ملاحظة: Pexels لا يرجع حقل "tags" موثوق لكل نتيجة (بعكس Pixabay)، لذلك
لا نطبّق عليه نفس خوارزمية relevance_score — نعتمد على ترتيب نتائج بحث
Pexels نفسها (دقيق بالعادة) بدل حساب تطابق يدوي.
"""
import requests
from scripts import config

PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"


def fetch_images(keyword: str, per_page: int = 3, orientation: str = "landscape") -> list[str]:
    """orientation المتوقعة من Pexels: landscape / portrait / square."""
    if not config.PEXELS_API_KEY:
        return []

    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": keyword, "per_page": max(1, per_page), "orientation": orientation}
    try:
        r = requests.get(PEXELS_PHOTO_SEARCH_URL, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        return [p["src"]["large2x"] for p in photos if p.get("src", {}).get("large2x")]
    except Exception as e:
        print(f"[PEXELS ERROR] فشل جلب صور Pexels لـ '{keyword}' (orientation={orientation}): {e}")
        return []


def fetch_videos(keyword: str, per_page: int = 3) -> list[dict]:
    """
    يرجع قائمة dicts فيها {"url", "width", "height"} — بدون فلتر اتجاه
    لنفس السبب الموجود بـ pixabay_provider (Remotion يقص تلقائياً).
    """
    if not config.PEXELS_API_KEY:
        return []

    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": keyword, "per_page": max(1, per_page)}
    try:
        r = requests.get(PEXELS_VIDEO_SEARCH_URL, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        videos = r.json().get("videos", [])
        results = []
        for v in videos:
            files = v.get("video_files", [])
            if not files:
                continue
            # نفضّل ملف بجودة hd (متوازن حجم/جودة) بدل uhd الثقيل جداً على
            # وقت رندرة GitHub Actions المحدود
            chosen = next((f for f in files if f.get("quality") == "hd"), None) or files[0]
            if not chosen.get("link"):
                continue
            results.append({
                "url": chosen["link"],
                "width": chosen.get("width", 0),
                "height": chosen.get("height", 0),
            })
        return results
    except Exception as e:
        print(f"[PEXELS ERROR] فشل جلب فيديوهات Pexels لـ '{keyword}': {e}")
        return []
