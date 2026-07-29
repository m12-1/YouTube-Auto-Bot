"""
المرحلة 4: البحث المتوازي عن الوسائط في Pexels + Pixabay لكل مشهد،
باستخدام visual_keywords (من المرحلة 3) مع youtube_keywords كبدائل احتياطية.

تم التعديل: تحسين التعامل مع NASA API:
- فحص المفتاح قبل الطلب.
- إضافة User-Agent مناسب.
- تعطيل المصدر فوراً عند 403 مع رسالة توضيحية.
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
    MAX_CANDIDATES_PER_VERIFICATION_BATCH,
    CONFLICTING_ENTITY_GROUPS,
)

logger = logging.getLogger("modules.media_downloader")

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/videos/"
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_METADATA_URL = "https://archive.org/metadata/{identifier}"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
NASA_SEARCH_URL = "https://images-api.nasa.gov/search"

# User-Agent لـ Wikimedia Commons
COMMONS_USER_AGENT = (
    "YouTube-Auto-Bot/1.0 "
    "(https://github.com/; contact: admin@example.com) "
    "python-aiohttp"
)
COMMONS_HEADERS = {"User-Agent": COMMONS_USER_AGENT}

# User-Agent لـ NASA (بعض APIs تتطلبه)
NASA_USER_AGENT = "Mozilla/5.0 (compatible; YouTubeAutoBot/1.0; +https://github.com/your-repo)"

_PER_SOURCE_QUOTA = CANDIDATES_PER_KEYWORD_PER_SOURCE

# ===== متغيرات حالة المصادر (Circuit Breaker) =====
_NASA_ENABLED = True   # سنفحص المفتاح عند أول استخدام
_PIXABAY_ENABLED = True
_NASA_KEY_CHECKED = False  # لمنع تكرار فحص المفتاح


def _mentions_space_or_astronomy(*texts: str) -> bool:
    combined = " ".join(t for t in texts if t).lower()
    if not combined:
        return False
    return any(kw.lower() in combined for kw in SPACE_ASTRONOMY_CATEGORY_KEYWORDS)


def _is_license_allowed_for_youtube(license_url: str = "", rights: str = "") -> tuple[bool, bool]:
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
    if any(a in cc_allowed for a in license_url):
        return True, True

    return False, False


def _find_entity_group(text: str) -> list[str] | None:
    text = (text or "").lower()
    if not text:
        return None
    for group in CONFLICTING_ENTITY_GROUPS:
        for subgroup in group:
            if any(name.lower() in text for name in subgroup):
                return subgroup
    return None


def _passes_entity_relevance(keyword: str, *texts: str) -> bool:
    keyword_group = _find_entity_group(keyword)
    if keyword_group is None:
        return True
    combined_meta = " ".join(t for t in texts if t)
    meta_group = _find_entity_group(combined_meta)
    if meta_group is None:
        return True
    return meta_group is keyword_group


def _meets_min_resolution(height) -> bool:
    return bool(height) and height >= MIN_ACCEPTABLE_VIDEO_HEIGHT


def _classify_orientation(width, height) -> str | None:
    if not width or not height:
        return None
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def _orientation_allowed(width, height) -> bool:
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
                        "title": None,
                        "description": f"Stock footage by {video.get('user', {}).get('name')}" if video.get("user") else None,
                    })
            return results
    except Exception as e:
        logger.warning("خطأ بحث Pexels عن '%s': %s", keyword, e)
        return []


async def _search_pixabay(session: aiohttp.ClientSession, keyword: str, per_page: int = _PER_SOURCE_QUOTA):
    global _PIXABAY_ENABLED
    if not _PIXABAY_ENABLED:
        return []

    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        logger.warning("PIXABAY_API_KEY غير موجود، تعطيل Pixabay لهذه الجولة.")
        _PIXABAY_ENABLED = False
        return []

    params = {"key": api_key, "q": keyword, "per_page": per_page}
    try:
        async with session.get(PIXABAY_SEARCH_URL, params=params, timeout=20) as resp:
            if resp.status in (401, 403, 429):
                logger.warning("Pixabay [%s] -> HTTP %s (تعطيل Pixabay لتوفير الوقت)", keyword, resp.status)
                _PIXABAY_ENABLED = False
                return []

            if resp.status != 200:
                logger.warning("Pixabay [%s] -> HTTP %s", keyword, resp.status)
                return []

            data = await resp.json()
            results = []
            for hit in data.get("hits", []):
                videos = hit.get("videos", {})
                best = videos.get("large")
                if best and (best.get("height") or 0) < 1080:
                    best = None
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
                        "title": None,
                        "description": f"Tags: {hit.get('tags')}" if hit.get("tags") else None,
                    })
            return results
    except Exception as e:
        logger.warning("خطأ بحث Pixabay عن '%s': %s", keyword, e)
        return []


async def _search_internet_archive(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    params = {
        "q": f'{keyword} AND mediatype:movies',
        "fl[]": ["title", "identifier", "licenseurl", "rights"],
        "rows": limit * 3,
        "page": 1,
        "output": "json",
    }
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
    except Exception as e:
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
        except Exception as e:
            logger.warning("خطأ جلب ميتاداتا Internet Archive لـ '%s': %s", identifier, e)
            continue

        file_url = None
        file_width = None
        file_height = None
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

        ia_description = (meta.get("metadata", {}) or {}).get("description")
        if not _passes_entity_relevance(keyword, doc.get("title"), ia_description):
            logger.info(
                "[تصفية كيان] استُبعد فيديو Internet Archive '%s' لأنه يذكر كيانًا مختلفًا عن الكلمة المفتاحية '%s'.",
                doc.get("title"), keyword,
            )
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
            "title": doc.get("title"),
            "description": (meta.get("metadata", {}) or {}).get("description"),
        })
        if len(results) >= limit:
            break
    return results


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


async def _search_wikimedia_commons(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
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
    except Exception as e:
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
        except Exception as e:
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

        height = imageinfo.get("height")
        width = imageinfo.get("width")
        if not _meets_min_resolution(height):
            continue
        if not _orientation_allowed(width, height):
            continue

        file_url = imageinfo.get("url")
        if not file_url:
            continue

        commons_description = _strip_html(ext.get("ImageDescription", {}).get("value", ""))
        if not _passes_entity_relevance(keyword, title, commons_description):
            logger.info(
                "[تصفية كيان] استُبعد فيديو Wikimedia Commons '%s' لأنه يذكر كيانًا مختلفًا عن الكلمة المفتاحية '%s'.",
                title, keyword,
            )
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
            "title": title,
            "description": _strip_html(ext.get("ImageDescription", {}).get("value", "")) or None,
        })
        if len(results) >= limit:
            break
    return results


async def _search_nasa(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    """
    البحث في NASA API مع فحص صحة المفتاح مسبقاً.
    إذا كان المفتاح مفقوداً أو أعاد 403، يتم تعطيل المصدر نهائياً لهذه الجولة.
    """
    global _NASA_ENABLED, _NASA_KEY_CHECKED
    if not _NASA_ENABLED:
        return []

    api_key = os.environ.get("Nasa_API_key")
    
    # فحص المفتاح مرة واحدة فقط
    if not _NASA_KEY_CHECKED:
        _NASA_KEY_CHECKED = True
        if not api_key:
            logger.warning("Nasa_API_key غير موجود في البيئة. تعطيل NASA لتوفير الوقت.")
            _NASA_ENABLED = False
            return []
        # نرسل طلب اختبار بسيط للتحقق من صحة المفتاح
        test_params = {"api_key": api_key, "q": "earth", "media_type": "video", "page": 1}
        headers = {"User-Agent": NASA_USER_AGENT}
        try:
            async with session.get(NASA_SEARCH_URL, params=test_params, headers=headers, timeout=10) as resp:
                if resp.status == 403:
                    logger.error(
                        "مفتاح NASA غير صالح (HTTP 403). تحقق من المفتاح في https://api.nasa.gov . "
                        "تعطيل NASA لهذه الجولة."
                    )
                    _NASA_ENABLED = False
                    return []
                if resp.status != 200:
                    logger.warning("فشل اختبار NASA API (HTTP %s). تعطيل مؤقت.", resp.status)
                    _NASA_ENABLED = False
                    return []
        except Exception as e:
            logger.warning("فشل اختبار NASA API: %s. تعطيل مؤقت.", e)
            _NASA_ENABLED = False
            return []

    if not _NASA_ENABLED:
        return []

    # البحث الفعلي
    query_phrase = f'"{keyword}"' if " " in keyword.strip() else keyword
    params = {"q": query_phrase, "media_type": "video", "api_key": api_key}
    headers = {"User-Agent": NASA_USER_AGENT}
    try:
        async with session.get(NASA_SEARCH_URL, params=params, headers=headers, timeout=20) as resp:
            if resp.status == 403:
                logger.warning("NASA [%s] -> HTTP 403 (المفتاح غير صالح). تعطيل NASA.", keyword)
                _NASA_ENABLED = False
                return []
            if resp.status != 200:
                logger.warning("NASA [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            items = data.get("collection", {}).get("items", [])
    except Exception as e:
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
        description = item_data.get("description") or ""
        keywords_field = " ".join(item_data.get("keywords") or [])
        if not _passes_entity_relevance(keyword, title, description, keywords_field):
            logger.info(
                "[تصفية كيان] استُبعد فيديو NASA '%s' لأنه يذكر كيانًا مختلفًا عن الكلمة المفتاحية '%s'.",
                title, keyword,
            )
            continue
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
            "title": title,
            "description": item_data.get("description"),
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
    all_keywords = list(visual_keywords)
    use_nasa = _mentions_space_or_astronomy(category, topic, " ".join(all_keywords))

    if use_nasa:
        async with aiohttp.ClientSession() as session:
            nasa_lists = await asyncio.gather(*[_search_nasa(session, kw) for kw in all_keywords])
        nasa_candidates = [c for sub in nasa_lists for c in sub]
        if len(nasa_candidates) >= MAX_CANDIDATES_PER_VERIFICATION_BATCH:
            logger.info(
                "المحتوى فضائي/فلكي: ناسا وحدها كافية (%d مرشح) — تخطي بقية المصادر لهذه الدفعة.",
                len(nasa_candidates),
            )
            return nasa_candidates
        logger.info(
            "المحتوى فضائي/فلكي: ناسا أرجعت %d مرشح فقط (أقل من %d المطلوبة) — "
            "استكمال البحث ببقية المصادر كخطة احتياطية.",
            len(nasa_candidates), MAX_CANDIDATES_PER_VERIFICATION_BATCH,
        )
    else:
        nasa_candidates = []

    async with aiohttp.ClientSession() as session:
        tasks = []
        for kw in all_keywords:
            tasks.append(_search_pexels(session, kw))
            tasks.append(_search_pixabay(session, kw))
            tasks.append(_search_internet_archive(session, kw))
            tasks.append(_search_wikimedia_commons(session, kw))
        results_lists = await asyncio.gather(*tasks)

    candidates = nasa_candidates + [c for sub in results_lists for c in sub]
    return candidates


async def download_candidate(candidate: dict, dest_dir: str, max_retries: int = 3) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    ext = ".mp4"
    filename = f"{candidate['source']}_{candidate['id']}{ext}"
    path = os.path.join(dest_dir, filename)
    if os.path.exists(path):
        return path

    dl_headers = COMMONS_HEADERS if candidate.get("source") == "wikimedia_commons" else None
    timeout = 120 if candidate.get("source") == "internet_archive" else 60

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(candidate["url"], headers=dl_headers, timeout=timeout) as resp:
                    resp.raise_for_status()
                    with open(path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            f.write(chunk)
            return path
        except Exception as e:
            last_error = e
            if os.path.exists(path):
                os.remove(path)
            if attempt < max_retries:
                logger.warning(
                    "فشل تحميل %s (محاولة %d/%d): %s — إعادة المحاولة.",
                    candidate.get("url"), attempt, max_retries, e,
                )
                await asyncio.sleep(2 * attempt)
    logger.warning("فشل تحميل %s نهائيًا بعد %d محاولات: %s", candidate.get("url"), max_retries, last_error)
    raise last_error


def search_scene_media_sync(visual_keywords, youtube_keywords=None, category="", topic=""):
    return asyncio.run(search_scene_media(visual_keywords, youtube_keywords, category=category, topic=topic))


if __name__ == "__main__":
    print(search_scene_media_sync(["black hole space", "galaxy stars"]))
