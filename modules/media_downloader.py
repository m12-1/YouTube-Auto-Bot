"""
المرحلة 4: البحث المتوازي عن الوسائط في Pexels + Pixabay لكل مشهد،
باستخدام visual_keywords (من المرحلة 3) مع youtube_keywords كبدائل احتياطية.
"""

import os
import asyncio
import logging
import aiohttp

from config import (
    CANDIDATES_PER_KEYWORD_PER_SOURCE,
    SPACE_ASTRONOMY_CATEGORY_KEYWORDS,
    MIN_ACCEPTABLE_VIDEO_HEIGHT,
    ALLOWED_VIDEO_ORIENTATIONS,
)

logger = logging.getLogger("modules.media_downloader")

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/videos/"
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_METADATA_URL = "https://archive.org/metadata/{identifier}"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
NASA_SEARCH_URL = "https://images-api.nasa.gov/search"

# سياسة Wikimedia (User-Agent Policy) تحظر الطلبات بدون User-Agent واضح يوضح
# هوية التطبيق ومعلومات تواصل، وترفضها بـ HTTP 403 (نقطة إصلاح مشكلة
# "Wikimedia Commons -> HTTP 403" الظاهرة في اللوج).
COMMONS_USER_AGENT = (
    "YouTube-Auto-Bot/1.0 "
    "(https://github.com/; contact: admin@example.com) "
    "python-aiohttp"
)
COMMONS_HEADERS = {"User-Agent": COMMONS_USER_AGENT}

# عدد المقاطع الافتراضي المطلوب جلبه من كل مصدر لكل كلمة مفتاحية (نقطة 4)
_PER_SOURCE_QUOTA = CANDIDATES_PER_KEYWORD_PER_SOURCE


def _mentions_space_or_astronomy(*texts: str) -> bool:
    """يتحقق إن كان أي من النصوص المُمرَّرة (الفئة، الموضوع، الكلمات المفتاحية
    للمشهد) يشير للفضاء/الفلك، لتفعيل مصدر ناسا. فحص الفئة وحدها غير كافٍ:
    فئة عامة مثل "علوم" قد يكون موضوعها الفعلي فلكيًا، لذلك نفحص كل النصوص
    المتاحة معًا (نقطة 4)."""
    combined = " ".join(t for t in texts if t).lower()
    if not combined:
        return False
    return any(kw.lower() in combined for kw in SPACE_ASTRONOMY_CATEGORY_KEYWORDS)


def _is_license_allowed_for_youtube(license_url: str = "", rights: str = "") -> tuple[bool, bool]:
    """
    يفلتر التراخيص المسموحة للاستخدام على يوتيوب فقط (Public Domain / CC0 /
    CC-BY / CC-BY-SA)، ويرفض أي رخصة تحمل NC (غير تجاري) أو ND (لا تعديلات).
    يعيد (مسموح, يتطلب_ذكر_المصدر).
    """
    license_url = (license_url or "").lower()
    rights = (rights or "").lower()

    if "publicdomain" in license_url or "cc0" in license_url:
        return True, False
    if "not_in_copyright" in rights or "public domain" in rights or "cc0" in rights:
        return True, False

    cc_block = ["by-nc", "nc-", "/nc/", "by-nd", "/nd/"]
    if any(b in license_url for b in cc_block):
        return False, False

    cc_allowed = ["creativecommons.org/licenses/by/", "creativecommons.org/licenses/by-sa/"]
    if any(a in license_url for a in cc_allowed):
        return True, True

    # ترخيص غير معروف/غير واضح -> إقصاء حذرًا (نفس منطق ملاحظة الفيديوهات الحرة)
    return False, False


def _meets_min_resolution(height) -> bool:
    """فلتر دقة موحّد (>=1080p) عبر كل المصادر. أي ارتفاع غير معروف (None) يُرفض
    حذرًا، بنفس منطق استبعاد الرخص غير الواضحة، لتفادي مسحات/ملفات قديمة
    منخفضة الجودة من Internet Archive أو Wikimedia Commons (نقطة 2 من الطلب
    الثاني: اتساق فلتر الجودة عبر كل المصادر)."""
    return bool(height) and height >= MIN_ACCEPTABLE_VIDEO_HEIGHT


def _classify_orientation(width, height) -> str | None:
    """يصنّف الاتجاه من الأبعاد: portrait (ارتفاع > عرض)، landscape (عرض >
    ارتفاع)، square (متساويان). يعيد None لو الأبعاد غير معروفة."""
    if not width or not height:
        return None
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def _orientation_allowed(width, height) -> bool:
    """يفلتر حسب config.ALLOWED_VIDEO_ORIENTATIONS (عمودي فقط حاليًا، قابل
    للتوسعة للأفقي لاحقًا بإضافة قيمة واحدة للقائمة). الأبعاد غير المعروفة عند
    مصدر البحث تُقبل هنا مؤقتًا وتُفلتَر لاحقًا بفحص فعلي عبر ffprobe بعد
    التحميل (انظر ai_media_verification._filter_by_actual_orientation)."""
    orientation = _classify_orientation(width, height)
    if orientation is None:
        return True
    return orientation in ALLOWED_VIDEO_ORIENTATIONS


async def _search_pexels(session: aiohttp.ClientSession, keyword: str, per_page: int = _PER_SOURCE_QUOTA):
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        return []
    headers = {"Authorization": api_key}
    params = {"query": keyword, "per_page": per_page, "orientation": "portrait"}
    try:
        async with session.get(PEXELS_SEARCH_URL, headers=headers, params=params, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("Pexels [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            results = []
            for video in data.get("videos", []):
                files = sorted(video.get("video_files", []), key=lambda f: f.get("width", 0), reverse=True)
                # الحد الأدنى المطلوب أصبح 1080p (بدل 720p سابقًا) بناءً على
                # طلب صريح: لا يجوز أن تقل دقة أي مصدر مقبول عن 1080، حتى لو
                # كان الأعلى المتاح لهذا الفيديو تحديدًا 720p فقط.
                files = [
                    f for f in files
                    if (f.get("height") or 0) >= 1080 and _orientation_allowed(f.get("width"), f.get("height"))
                ]
                if files:
                    results.append({
                        "source": "pexels",
                        "keyword": keyword,
                        "id": str(video["id"]),
                        "url": files[0]["link"],
                        "width": files[0].get("width"),
                        "height": files[0].get("height"),
                        "duration": video.get("duration"),
                    })
            return results
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث Pexels عن '%s': %s", keyword, e)
        return []


async def _search_pixabay(session: aiohttp.ClientSession, keyword: str, per_page: int = _PER_SOURCE_QUOTA):
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        return []
    params = {"key": api_key, "q": keyword, "per_page": per_page}
    try:
        async with session.get(PIXABAY_SEARCH_URL, params=params, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("Pixabay [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            results = []
            for hit in data.get("hits", []):
                videos = hit.get("videos", {})
                # الحد الأدنى أصبح 1080p: نقبل طبقة "large" فقط (عادة
                # ~1920x1080 في Pixabay)، ونرفض "medium" (~720p) أيضًا الآن
                # بعد أن كان مقبولاً سابقًا. إن لم تتوفر "large" لهذه
                # الكلمة، لا نعيد هذا المرشح إطلاقًا بدل التنازل عن الدقة.
                best = videos.get("large")
                # بعض فيديوهات Pixabay تصنَّف "large" لكنها أقل فعليًا من
                # 1080p (نادر لكن يحدث)، لذا نتحقق من height الفعلي أيضًا
                # لا الاسم فقط.
                if best and (best.get("height") or 0) < 1080:
                    best = None
                # Pixabay لا يوفّر باراميتر orientation عند البحث (خلافًا
                # لـPexels)، لذلك فلتر الاتجاه هنا ضروري فعليًا وليس احتياطيًا فقط.
                if best and not _orientation_allowed(best.get("width"), best.get("height")):
                    best = None
                if best:
                    results.append({
                        "source": "pixabay",
                        "keyword": keyword,
                        "id": str(hit["id"]),
                        "url": best["url"],
                        "width": best.get("width"),
                        "height": best.get("height"),
                        "duration": hit.get("duration"),
                    })
            return results
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث Pixabay عن '%s': %s", keyword, e)
        return []


async def _search_internet_archive(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    """يبحث في Internet Archive عن فيديوهات برخصة مسموحة ليوتيوب فقط (نقطة 5)."""
    params = {
        "q": f'{keyword} AND mediatype:movies',
        "fl[]": ["title", "identifier", "licenseurl", "rights"],
        "rows": limit * 3,  # نجلب أكثر لتعويض ما يُستبعد بالفلترة، ثم نقص لـ limit
        "page": 1,
        "output": "json",
    }
    # مفاتيح S3 (S3_ACCESS_KEY / S3_SECRET_KEY) لا تلزم للبحث/القراءة العامة عن
    # الميتاداتا، وتُستخدم فقط لو احتجنا وصولًا خاصًا لاحقًا (تحميل مصادَق عليه).
    access_key = os.environ.get("S3_ACCESS_KEY")
    secret_key = os.environ.get("S3_SECRET_KEY")
    headers = {}
    if access_key and secret_key:
        headers["Authorization"] = f"LOW {access_key}:{secret_key}"
    try:
        async with session.get(IA_SEARCH_URL, params=params, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("Internet Archive [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            docs = data.get("response", {}).get("docs", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث Internet Archive عن '%s': %s", keyword, e)
        return []

    results = []
    for doc in docs:
        allowed, needs_attr = _is_license_allowed_for_youtube(doc.get("licenseurl"), doc.get("rights"))
        if not allowed:
            continue
        identifier = doc.get("identifier")
        if not identifier:
            continue
        try:
            async with session.get(IA_METADATA_URL.format(identifier=identifier), headers=headers, timeout=20) as meta_resp:
                if meta_resp.status != 200:
                    continue
                meta = await meta_resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("خطأ جلب ميتاداتا Internet Archive لـ '%s': %s", identifier, e)
            continue

        file_url = None
        file_width = None
        file_height = None
        # نفضّل مشتقات mp4/webm الحديثة (غالبًا الوحيدة التي تحمل width/height
        # موثوقة في الميتاداتا)، ونطبّق فلتر الدقة الموحّد؛ نتجاهل صيغ المسح
        # القديمة (.mpg/.mpeg/.ogv) لأنها غالبًا لا تبلغ 1080p أصلًا.
        for f in meta.get("files", []):
            name = f.get("name", "")
            if not name.lower().endswith((".mp4", ".webm")):
                continue
            height = f.get("height")
            height = int(height) if height and str(height).isdigit() else None
            width = f.get("width")
            width = int(width) if width and str(width).isdigit() else None
            if not _meets_min_resolution(height):
                continue
            if not _orientation_allowed(width, height):
                continue
            file_url = f"https://archive.org/download/{identifier}/{name}"
            file_width = width
            file_height = height
            break
        if not file_url:
            continue

        results.append({
            "source": "internet_archive",
            "keyword": keyword,
            "id": identifier,
            "url": file_url,
            "width": file_width,
            "height": file_height,
            "duration": None,
            "requires_attribution": needs_attr,
            "attribution_text": f"\"{doc.get('title', identifier)}\" via Internet Archive" if needs_attr else None,
        })
        if len(results) >= limit:
            break
    return results


async def _search_wikimedia_commons(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    """يبحث في Wikimedia Commons عن فيديوهات حرة (بدون مفتاح API) (نقطة 6)."""
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": f"{keyword} filetype:video",
        "srnamespace": 6,
        "srlimit": limit * 3,
        "format": "json",
    }
    try:
        async with session.get(COMMONS_API_URL, params=search_params, headers=COMMONS_HEADERS, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("Wikimedia Commons [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            search_results = data.get("query", {}).get("search", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث Wikimedia Commons عن '%s': %s", keyword, e)
        return []

    results = []
    for res in search_results:
        title = res.get("title")
        if not title:
            continue
        info_params = {
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
            "format": "json",
        }
        try:
            async with session.get(COMMONS_API_URL, params=info_params, headers=COMMONS_HEADERS, timeout=20) as info_resp:
                if info_resp.status != 200:
                    continue
                info_data = await info_resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("خطأ جلب معلومات ملف Wikimedia Commons '%s': %s", title, e)
            continue

        pages = info_data.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        imageinfo_list = page.get("imageinfo") or [{}]
        imageinfo = imageinfo_list[0]
        mime = imageinfo.get("mime") or ""
        if not mime.startswith("video/"):
            continue

        ext = imageinfo.get("extmetadata", {})
        license_short = ext.get("LicenseShortName", {}).get("value", "")
        license_url = ext.get("LicenseUrl", {}).get("value", "")
        artist = ext.get("Artist", {}).get("value", "")

        allowed, needs_attr = _is_license_allowed_for_youtube(license_url or license_short)
        if not allowed:
            continue

        # فلتر الدقة الموحّد (>=1080p)، نفس المطبّق على كل المصادر الأخرى -
        # يستبعد سكانات الفيديو القديمة/منخفضة الدقة الشائعة في Commons.
        height = imageinfo.get("height")
        width = imageinfo.get("width")
        if not _meets_min_resolution(height):
            continue
        if not _orientation_allowed(width, height):
            continue

        file_url = imageinfo.get("url")
        if not file_url:
            continue

        results.append({
            "source": "wikimedia_commons",
            "keyword": keyword,
            "id": title,
            "url": file_url,
            "width": imageinfo.get("width"),
            "height": height,
            "duration": None,
            "requires_attribution": needs_attr,
            "attribution_text": f"\"{title}\" by {artist or 'Wikimedia Commons contributor'} via Wikimedia Commons" if needs_attr else None,
        })
        if len(results) >= limit:
            break
    return results


async def _search_nasa(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    """يبحث في مكتبة ناسا للوسائط عن فيديوهات فضاء/فلك (نقطة 2). محتوى ناسا
    عمومًا في المجال العام (Public Domain) ولا يحتاج ذكر مصدر إلزاميًا، لكننا
    نضيف إسنادًا احترامًا للعرف."""
    api_key = os.environ.get("Nasa_API_key")
    if not api_key:
        return []
    params = {"q": keyword, "media_type": "video", "api_key": api_key}
    try:
        async with session.get(NASA_SEARCH_URL, params=params, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("NASA [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            items = data.get("collection", {}).get("items", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث NASA عن '%s': %s", keyword, e)
        return []

    results = []
    for item in items:
        item_data = (item.get("data") or [{}])[0]
        links = item.get("links") or []
        video_link = next((l.get("href") for l in links if l.get("href", "").endswith((".mp4", ".webm"))), None)
        if not video_link:
            continue
        title = item_data.get("title", "NASA video")
        results.append({
            "source": "nasa",
            "keyword": keyword,
            "id": item_data.get("nasa_id", title),
            "url": video_link,
            "width": None,
            "height": None,
            "duration": None,
            "requires_attribution": True,
            "attribution_text": f"\"{title}\" courtesy of NASA",
        })
        if len(results) >= limit:
            break
    return results


async def search_scene_media(
    visual_keywords: list[str],
    youtube_keywords: list[str] | None = None,
    category: str = "",
    topic: str = "",
):
    """
    يبحث بالتوازي في Pexels وPixabay وInternet Archive وWikimedia Commons
    (وناسا إن كانت الفئة/الموضوع/كلمات المشهد تخص الفضاء/الفلك) باستخدام كل
    الكلمات المفتاحية للمشهد، ويعيد قائمة موحّدة من المرشحين (بدون تحميل
    الملفات بعد). لكل كلمة مفتاحية نجلب حتى 3 مقاطع من كل مصدر (نقطة 4).
    """
    all_keywords = list(visual_keywords) + list(youtube_keywords or [])
    # نفحص الفئة والموضوع وكلمات المشهد نفسها معًا (وليس الفئة وحدها)، لأن
    # فئة عامة مثل "علوم" قد يكون موضوعها الفعلي فلكيًا (نقطة 4).
    use_nasa = _mentions_space_or_astronomy(category, topic, " ".join(all_keywords))

    async with aiohttp.ClientSession() as session:
        tasks = []
        for kw in all_keywords:
            tasks.append(_search_pexels(session, kw))
            tasks.append(_search_pixabay(session, kw))
            tasks.append(_search_internet_archive(session, kw))
            tasks.append(_search_wikimedia_commons(session, kw))
            if use_nasa:
                tasks.append(_search_nasa(session, kw))
        results_lists = await asyncio.gather(*tasks)

    candidates = [c for sub in results_lists for c in sub]
    return candidates


async def download_candidate(candidate: dict, dest_dir: str) -> str:
    """يحمّل ملف الفيديو الفعلي لمرشح معيّن ويعيد المسار المحلي."""
    os.makedirs(dest_dir, exist_ok=True)
    ext = ".mp4"
    filename = f"{candidate['source']}_{candidate['id']}{ext}"
    path = os.path.join(dest_dir, filename)
    if os.path.exists(path):
        return path

    dl_headers = COMMONS_HEADERS if candidate.get("source") == "wikimedia_commons" else None
    async with aiohttp.ClientSession() as session:
        async with session.get(candidate["url"], headers=dl_headers, timeout=60) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
    return path


def search_scene_media_sync(visual_keywords, youtube_keywords=None, category="", topic=""):
    return asyncio.run(search_scene_media(visual_keywords, youtube_keywords, category=category, topic=topic))


if __name__ == "__main__":
    print(search_scene_media_sync(["black hole space", "galaxy stars"]))
