"""
asset_fetcher.py
مهمة هذا الملف: تنزيل ملفات الوسائط فعلياً (صور/فيديوهات) + توزيع طلبات
البحث بين مزوّدَي الوسائط المتاحين: Pixabay (pixabay_provider.py) و
Pexels (pexels_provider.py).

كان المشروع يعتمد على Pixabay حصراً. الآن مع وجود PEXELS_API_KEY صالح،
كلا المزودين يعملان معاً: لكل مشهد نختار ترتيب تجربة عشوائي بين المزودين
(توزيع تقريبي 50/50 لمنع استنزاف حصة أي واحد منهم لوحده)، ولو فشل أو لم
يرجع نتائج كافية المزود الأول، نجرب الثاني تلقائياً كاحتياط — بدل التوقف.

فحص التطابق البصري (Gemini + احتياطي CLIP المحلي) انتقل بالكامل إلى ملف
media_relevance_checker.py (مهمة منفصلة عن التنزيل نفسه).
"""
import random

from scripts import config, pixabay_provider, pexels_provider

# نسبة تفضيل الفيديو مقابل الصورة لكل مشهد (0.70 = يحاول فيديو أولاً بـ 70%
# من المشاهد و30% صور، حسب الطلب: فيديوهات 70% / صور 30%)
VIDEO_PREFERENCE_RATIO = 0.70

# أقل درجة تطابق (0-1) نقبلها بين الكلمة المفتاحية ووسوم Pixabay قبل اعتبار
# النتيجة "غير ذات صلة كافية" (Pexels لا يخضع لهذا الفحص، راجع pexels_provider.py)
MIN_RELEVANCE_SCORE = 0.2


def _available_providers() -> list[str]:
    """يرجع قائمة المزودين المتاحين فعلياً (عندهم مفتاح API صالح) بترتيب
    عشوائي في كل استدعاء، لتوزيع العبء بينهما بدل الاعتماد الدائم على
    مزوّد واحد. لو مزود واحد فقط متاح، يرجع هو فقط (لا كسر بالتوافق)."""
    providers = []
    if config.PIXABAY_API_KEY:
        providers.append("pixabay")
    if config.PEXELS_API_KEY:
        providers.append("pexels")
    random.shuffle(providers)
    return providers


def _videos_from_provider(provider: str, keywords: list[str], target_count: int) -> tuple[list[dict], float]:
    """يرجع (نتائج الفيديو, أفضل درجة تطابق وُجدت). Pexels لا يملك درجة
    تطابق موثوقة فنعيد 1.0 له إذا وجد أي نتيجة (نثق بترتيب بحثه)."""
    if provider == "pixabay":
        best_score, best_vids = -1.0, None
        for kw in keywords:
            vids = pixabay_provider.fetch_videos(kw, per_page=target_count)
            if vids and vids[0]["score"] > best_score:
                best_score, best_vids = vids[0]["score"], vids
        return (best_vids or []), best_score

    if provider == "pexels":
        for kw in keywords:
            vids = pexels_provider.fetch_videos(kw, per_page=target_count)
            if vids:
                return vids, 1.0
        return [], -1.0

    return [], -1.0


def _try_videos_across_providers(keywords: list[str], target_count: int, providers: list[str]) -> list[dict]:
    for provider in providers:
        vids, score = _videos_from_provider(provider, keywords, target_count)
        if not vids:
            continue
        if provider == "pixabay" and score < MIN_RELEVANCE_SCORE:
            print(f"[ASSET WARNING] أفضل تطابق فيديو Pixabay لكلمات {keywords} كانت درجته "
                  f"{score:.2f} (أقل من الحد الأدنى {MIN_RELEVANCE_SCORE})، تجربة المزود التالي إن وجد...")
            continue
        return [{"type": "video", "url": v["url"], "provider": provider} for v in vids[:target_count]]
    return []


def _try_images_across_providers(keywords: list[str], target_count: int, is_short: bool,
                                  providers: list[str]) -> list[dict]:
    pixabay_orientation = "vertical" if is_short else "horizontal"
    pexels_orientation = "portrait" if is_short else "landscape"

    for provider in providers:
        images = []
        if provider == "pixabay":
            for kw in keywords:
                images += pixabay_provider.fetch_images(kw, per_page=target_count, orientation=pixabay_orientation)
                if len(images) >= target_count:
                    break
        elif provider == "pexels":
            for kw in keywords:
                images += pexels_provider.fetch_images(kw, per_page=target_count, orientation=pexels_orientation)
                if len(images) >= target_count:
                    break
        if images:
            return [{"type": "image", "url": u, "provider": provider} for u in images[:target_count]]
    return []


def fetch_pixabay(keyword: str, per_page: int = 3, orientation: str = "horizontal") -> list[str]:
    """يُبقى للتوافق الخلفي (thumbnail_generator.py يستخدمه مباشرة)."""
    return pixabay_provider.fetch_images(keyword, per_page=per_page, orientation=orientation)


def fetch_pexels(keyword: str, per_page: int = 3, orientation: str = "landscape") -> list[str]:
    """يُبقى للتوافق الخلفي لأي كود يستدعيه مباشرة بالاسم القديم."""
    return pexels_provider.fetch_images(keyword, per_page=per_page, orientation=orientation)


def get_images_for_scene(keywords: list[str], target_count: int = 4,
                          is_short: bool = True) -> list[str]:
    """
    is_short=True يطلب صوراً عمودية (تناسب 1080x1920 بدون قص كبير)، وإلا يطلب
    أفقية. توزّع البحث بين Pixabay و Pexels، ولو فشل كلاهما بالكلمات
    المحددة، تجرب كلمات احتياطية عامة بدل إرجاع قائمة فارغة.
    """
    providers = _available_providers()
    if not providers:
        return []

    media = _try_images_across_providers(keywords, target_count, is_short, providers)
    if not media:
        for fallback_kw in ["abstract background", "nature", "sky"]:
            media = _try_images_across_providers([fallback_kw], target_count, is_short, providers)
            if media:
                break

    return [m["url"] for m in media][:target_count]


def fetch_pixabay_videos(keyword: str, per_page: int = 3) -> list[dict]:
    """يُبقى للتوافق الخلفي؛ التوزيع الفعلي بين المزودين صار بـ get_media_for_scene."""
    return pixabay_provider.fetch_videos(keyword, per_page=per_page)


def get_media_for_scene(keywords: list[str], target_count: int = 1,
                         is_short: bool = True, prefer_video: bool = True,
                         topic_context: str = "") -> list[dict]:
    """
    ترجع قائمة عناصر media بالشكل {"type": "video"|"image", "url": "...", "provider": "..."}.

    topic_context: الموضوع الرئيسي للفيديو — يُضاف لكل كلمة مفتاحية لضمان
    بقاء النتائج ضمن السياق الصحيح (مثلاً: "survival" → "survival gaming"
    بدل مقاطع طبيعة عن البقاء في البرية).

    المنطق: نجرب مزوّداً بترتيب عشوائي (توزيع العمل بين Pixabay و Pexels)
    عبر كل الكلمات المفتاحية للمشهد؛ لو المزود الأول ما رجّع نتيجة كافية
    (أو تحت حد التطابق الأدنى لـ Pixabay)، ننتقل تلقائياً للمزود الثاني.
    """
    providers = _available_providers()
    if not providers:
        print("[ASSET ERROR] لا يوجد أي مفتاح API صالح لا لـ Pixabay ولا لـ Pexels.")
        return []

    media: list[dict] = []

    # إرفاق الموضوع الرئيسي مع كل كلمة مفتاحية لمنع الانحراف عن السياق
    contextualized_keywords = []
    for kw in keywords:
        if topic_context:
            contextualized_keywords.append(f"{kw} {topic_context}".strip())
        contextualized_keywords.append(kw)  # نضيف الكلمة وحدها أيضاً كبديل

    if prefer_video:
        media = _try_videos_across_providers(contextualized_keywords, target_count, providers)

        # لو فشلت كل الكلمات مع كل المزودين، جرب كلمات عامة للفيديو قبل السقوط للصور
        if not media:
            for fallback_vid_kw in ["cinematic", "aerial", "timelapse", "urban", "nature footage"]:
                media = _try_videos_across_providers([fallback_vid_kw], target_count, providers)
                if media:
                    break

    if not media:
        media = _try_images_across_providers(contextualized_keywords, target_count, is_short, providers)

    if not media:
        for fallback_kw in ["abstract background", "nature", "sky"]:
            media = _try_images_across_providers([fallback_kw], target_count, is_short, providers)
            if media:
                break

    return media[:target_count]


def download_video(url: str, out_path: str):
    """يرجع المسار لو نجح التحميل فعلياً، أو None لو فشل — بدل تمرير رابط
    ميت للرندرة."""
    import requests
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
    """يرجع المسار لو نجح التحميل فعلياً، أو None لو فشل."""
    import requests
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return out_path
    except Exception as e:
        print(f"[ASSET ERROR] فشل تحميل الصورة من {url}: {e}")
        return None
