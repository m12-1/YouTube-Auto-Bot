"""
asset_fetcher.py
يعتمد على Pixabay حصرياً حالياً (Pexels معطّل بسبب مشكلة مفتاح سابقة، يمكن
إعادة تفعيله لاحقاً بـ fetch_pexels أدناه إذا صار المفتاح صالحاً).

إصلاحات هذه النسخة:
- إزالة رابط placeholder وهمي غير حقيقي كان يسبب فشل تحميل صامت
- دعم orientation ديناميكي (عمودي للشورت، أفقي للطويل) بدل "horizontal" ثابت
- get_images_for_scene يرجع فقط روابط، والتحقق من نجاح التحميل الفعلي بـ download_image
"""
import requests
from requests.utils import quote
from scripts import config

MIN_WIDTH = 1080  # مخفّض من 1920 لأن أغلب صور Pixabay العمودية أضيق من هذا


def fetch_pixabay(keyword: str, per_page: int = 3, orientation: str = "horizontal") -> list[str]:
    if not config.PIXABAY_API_KEY:
        return []

    encoded_keyword = quote(keyword)
    url = (
        f"https://pixabay.com/api/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}"
        f"&image_type=photo&orientation={orientation}&per_page={per_page}&safesearch=true"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        return [h["largeImageURL"] for h in hits]
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pixabay لـ '{keyword}' (orientation={orientation}): {e}")
        return []


def fetch_pexels(keyword: str, per_page: int = 3, orientation: str = "landscape") -> list[str]:
    if not config.PEXELS_API_KEY:
        return []
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": keyword, "per_page": per_page, "orientation": orientation}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        return [p["src"]["large2x"] for p in photos]
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pexels لـ '{keyword}': {e}")
        return []


def get_images_for_scene(keywords: list[str], target_count: int = 4,
                          is_short: bool = True) -> list[str]:
    """
    is_short=True يطلب صوراً عمودية (تناسب 1080x1920 بدون قص كبير)، وإلا يطلب
    أفقية. لو الكلمة المحددة ما رجعت نتائج، يجرب كلمات احتياطية عامة بدل ما
    يرجع placeholder وهمي غير قابل للتحميل (كان هذا الخطأ بالنسخة السابقة).
    """
    orientation = "vertical" if is_short else "horizontal"
    images = []
    for kw in keywords:
        images += fetch_pixabay(kw, per_page=target_count, orientation=orientation)
        if len(images) >= target_count:
            break

    if not images:
        for fallback_kw in ["abstract background", "nature", "sky"]:
            images += fetch_pixabay(fallback_kw, per_page=target_count, orientation=orientation)
            if images:
                break

    return images[:target_count]


def download_image(url: str, out_path: str):
    """يرجع المسار لو نجح التحميل فعلياً، أو None لو فشل — لازم يُفحص بالمستدعي
    قبل إضافته لقائمة الصور، وإلا يتسبب بفشل صامت لاحقاً بالرندرة."""
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return out_path
    except Exception as e:
        print(f"[ASSET ERROR] فشل تحميل الصورة من {url}: {e}")
        return None
