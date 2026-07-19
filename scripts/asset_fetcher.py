"""
asset_fetcher.py
يعتمد على Pixabay حصرياً حالياً (Pexels معطّل بسبب مشكلة مفتاح سابقة، يمكن
إعادة تفعيله لاحقاً بـ fetch_pexels أدناه إذا صار المفتاح صالحاً).

إصلاحات هذه النسخة:
- إزالة رابط placeholder وهمي غير حقيقي كان يسبب فشل تحميل صامت
- دعم orientation ديناميكي (عمودي للشورت، أفقي للطويل) بدل "horizontal" ثابت
- get_images_for_scene يرجع فقط روابط، والتحقق من نجاح التحميل الفعلي بـ download_image

إضافة جديدة (مزيج فيديو + صور):
- fetch_pixabay_videos(): نفس مفتاح Pixabay الحالي، بدون أي سر إضافي مطلوب
  بـ GitHub Secrets، يجلب مقاطع فيديو حرة الحقوق (footage) بدل صور ثابتة فقط.
- get_media_for_scene(): تعطي كل مشهد "وسيط" (video أو image) بدل صورة فقط،
  مع fallback تلقائي لصورة لو ما لقى فيديو مناسب للكلمة المفتاحية — هذا هو
  التغيير الأساسي اللي يكسر شكل "عرض الشرائح" ويقرّب الفيديو من مونتاج بشري.
"""
import random

import requests
from requests.utils import quote
from scripts import config

MIN_WIDTH = 1080  # مخفّض من 1920 لأن أغلب صور Pixabay العمودية أضيق من هذا

# نسبة تفضيل الفيديو مقابل الصورة لكل مشهد (0.95 = يحاول فيديو أولاً بـ 95%
# من المشاهد، لتقريب الإنتاج لمونتاج بشري حيّ بدل الصور الثابتة)
VIDEO_PREFERENCE_RATIO = 0.95


def fetch_pixabay(keyword: str, per_page: int = 3, orientation: str = "horizontal") -> list[str]:
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


def fetch_pixabay_videos(keyword: str, per_page: int = 3, orientation: str = "horizontal") -> list[dict]:
    """
    يستخدم نفس PIXABAY_API_KEY الموجود أصلاً (لا يحتاج سر جديد بـ Secrets).
    يرجع قائمة dicts فيها {"url": ..., "width": ..., "height": ...} بدل روابط
    فقط، لأننا نحتاج الأبعاد لاحقاً لمعرفة هل المقطع يغطي الإطار بدون تشويه.

    ملاحظة: video_type="film" يعطي لقطات سينمائية (b-roll) بدل "animation"
    (رسوم متحركة/موشن جرافيك) اللي ما يناسب أسلوب القناة الواقعي.
    """
    if not config.PIXABAY_API_KEY:
        return []

    # إصلاح الخطأ 400: واجهة Pixabay ترفض أي قيمة أقل من 3
    api_per_page = max(3, per_page)

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
            # نفضّل "medium" (توازن جيد بين الجودة وحجم الملف لفيديو شورت
            # قصير)، ولو غير متوفر نرجع لـ "small" ثم "large" كبديل
            chosen = videos.get("medium") or videos.get("small") or videos.get("large")
            if not chosen or not chosen.get("url"):
                continue
            width, height = chosen.get("width", 0), chosen.get("height", 0)
            # فلترة اتجاه تقريبية يدوياً لأن Pixabay Video API لا يدعم
            # فلتر orientation مباشر بعكس API الصور
            is_vertical_clip = height >= width
            wants_vertical = orientation == "vertical"
            if wants_vertical != is_vertical_clip:
                continue
            results.append({"url": chosen["url"], "width": width, "height": height})
        return results
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pixabay Video لـ '{keyword}' (orientation={orientation}): {e}")
        return []


def get_media_for_scene(keywords: list[str], target_count: int = 1,
                         is_short: bool = True, prefer_video: bool = True) -> list[dict]:
    """
    نسخة "مزيج" من get_images_for_scene: ترجع قائمة عناصر media بالشكل
    {"type": "video"|"image", "url": "..."} بدل صور فقط.

    المنطق: لو prefer_video=True (يُقرَّر عشوائياً لكل مشهد بـ VIDEO_PREFERENCE_RATIO
    بالسكربت المستدعي)، يحاول أول كلمة مفتاحية بفيديو، ولو ما رجعت نتيجة
    (كلمات مجردة كثيرة ما عندها لقطات فيديو مناسبة) يجرب الكلمة اللي بعدها،
    وبالنهاية يتراجع للصور تلقائياً بدل ما يفشل السيناريو كامل.
    """
    orientation = "vertical" if is_short else "horizontal"
    media: list[dict] = []

    if prefer_video:
        for kw in keywords:
            vids = fetch_pixabay_videos(kw, per_page=target_count, orientation=orientation)
            if vids:
                media = [{"type": "video", "url": v["url"]} for v in vids[:target_count]]
                break
        
        # لو فشلت كل الكلمات الخاصة بالمشهد، جرب كلمات عامة للفيديو قبل السقوط للصور
        if not media:
            for fallback_vid_kw in ["cinematic", "aerial", "timelapse", "urban", "nature footage"]:
                vids = fetch_pixabay_videos(fallback_vid_kw, per_page=target_count, orientation=orientation)
                if vids:
                    media = [{"type": "video", "url": v["url"]} for v in vids[:target_count]]
                    break

    if not media:
        images = get_images_for_scene(keywords, target_count=target_count, is_short=is_short)
        media = [{"type": "image", "url": u} for u in images]

    if not media:
        # نفس الكلمات الاحتياطية العامة المستخدمة بالصور، هذه المرة كخط
        # دفاع أخير حتى لو فشل كل شي فوق
        for fallback_kw in ["abstract background", "nature", "sky"]:
            images = get_images_for_scene([fallback_kw], target_count=target_count, is_short=is_short)
            if images:
                media = [{"type": "image", "url": u} for u in images]
                break

    return media[:target_count]


def download_video(url: str, out_path: str):
    """نفس منطق download_image لكن للفيديو — يرجع None لو فشل التحميل فعلياً
    بدل تمرير رابط ميت للرندرة (نفس درس الإصلاح السابق مع الصور)."""
    try:
        r = requests.get(url, timeout=40, stream=True)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        return out_path
    except Exception as e:
        print(f"[ASSET ERROR] فشل تحميل الفيديو من {url}: {e}")
        return None


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
