"""
المرحلة 4: البحث المتوازي عن الوسائط في Pexels + Pixabay لكل مشهد،
باستخدام visual_keywords (من المرحلة 3) مع youtube_keywords كبدائل احتياطية.
تم التعديل: إضافة health check لـ NASA، وفي حال فشل بـ 403 (CloudFront) يتم تعطيل المصدر نهائياً.
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

# ===== سياسة User-Agent الموحدة =====
DEFAULT_USER_AGENT = (
    "YouTube-Auto-Bot/1.0 "
    "(https://github.com/your-repo; contact: admin@example.com) "
    "python-aiohttp"
)
COMMONS_HEADERS = {"User-Agent": DEFAULT_USER_AGENT}
NASA_HEADERS = {"User-Agent": DEFAULT_USER_AGENT}

_PER_SOURCE_QUOTA = CANDIDATES_PER_KEYWORD_PER_SOURCE

# ===== متغيرات حالة المصادر =====
_NASA_ENABLED = True      # سيتم تعطيله تلقائياً عند فشل health check
_PIXABAY_ENABLED = True
_NASA_HEALTH_CHECK_DONE = False  # لمنع تكرار الفحص


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
            logger.info("Pexels [%s] فيديو: %d نتيجة (بعد الفلاتر) من %d.", keyword, len(results), len(data.get("videos", [])))
            return results
    except Exception as e:
        logger.warning("خطأ بحث Pexels عن '%s': %s", keyword, e)
        return []


async def _search_pixabay(session: aiohttp.ClientSession, keyword: str, per_page: int = _PER_SOURCE_QUOTA):
    global _PIXABAY_ENABLED
    if not _PIXABAY_ENABLED:
        logger.info("Pixabay [%s]: تخطي (المصدر معطّل حاليًا لهذا التشغيل).", keyword)
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
            logger.info("Pixabay [%s] فيديو: %d نتيجة (بعد الفلاتر) من %d.", keyword, len(results), len(data.get("hits", [])))
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
        # تم تصحيح الخطأ هنا: إزالة القوس الزائد
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


async def _nasa_health_check(session: aiohttp.ClientSession) -> bool:
    """
    يجري فحصاً واحداً لتحديد إن كان NASA متاحاً من هذه البيئة.
    يعيد True إن نجح، False إن فشل بـ 403 CloudFront أو أي خطأ آخر.
    """
    try:
        async with session.get(
            NASA_SEARCH_URL,
            params={"q": "earth", "media_type": "video", "api_key": "DEMO_KEY"},
            headers=NASA_HEADERS,
            timeout=10
        ) as resp:
            if resp.status == 403:
                text = await resp.text()
                if "Request blocked" in text or "CloudFront" in text:
                    logger.error("NASA محظور بواسطة CloudFront/WAF. سيتم تعطيل NASA نهائياً.")
                    return False
                else:
                    logger.warning("NASA 403 غير معتاد (ليس CloudFront). سيتم تعطيل NASA احتياطياً.")
                    return False
            elif resp.status == 200:
                logger.info("NASA متاح. سيتم استخدامه للمواضيع الفضائية.")
                return True
            else:
                logger.warning("NASA health check فشل بـ HTTP %s. سيتم تعطيل NASA.", resp.status)
                return False
    except Exception as e:
        logger.warning("NASA health check استثناء: %s. سيتم تعطيل NASA.", e)
        return False


async def _search_nasa(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    global _NASA_ENABLED, _NASA_HEALTH_CHECK_DONE

    if not _NASA_ENABLED:
        logger.info("NASA [%s]: تخطي (المصدر معطّل حاليًا لهذا التشغيل).", keyword)
        return []

    # تشغيل health check مرة واحدة عند أول استدعاء لـ NASA
    if not _NASA_HEALTH_CHECK_DONE:
        _NASA_HEALTH_CHECK_DONE = True
        if not await _nasa_health_check(session):
            _NASA_ENABLED = False
            logger.info("تم تعطيل NASA بناءً على فشل health check.")
            return []

    # إذا وصلنا إلى هنا، فإن NASA مفعّل
    api_key = os.environ.get("Nasa_API_key")
    if not api_key:
        logger.warning("Nasa_API_key غير موجود، تعطيل NASA.")
        _NASA_ENABLED = False
        return []

    params = {
        "q": keyword,
        "media_type": "video",
        "api_key": api_key,
    }
    try:
        async with session.get(NASA_SEARCH_URL, params=params, headers=NASA_HEADERS, timeout=15) as resp:
            if resp.status == 403:
                logger.warning("NASA فجأة أعاد 403 بعد health check. تعطيل.")
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
    logger.info("NASA [%s] فيديو: %d نتيجة (بعد الفلاتر) من %d.", keyword, len(results), len(items))
    return results


# ============================================================================
# ===== البحث عن الصور (خطة بديلة صارمة عند فشل إيجاد فيديو مناسب لمشهد) =====
# ============================================================================
# نفس بنية دوال بحث الفيديو أعلاه تمامًا (نفس المصادر، نفس فلاتر الرخصة/الدقة/
# الاتجاه/تعارض الكيانات)، لكن موجّهة لنقاط نهاية الصور بدل الفيديو في كل مصدر.
# لا تُستدعى هذه الدوال إطلاقًا إلا بعد فشل كل محاولات الفيديو العادية لمشهد
# معيّن (انظر ai_media_verification.verify_scene_media)، والتحقق منها لاحقًا
# يخضع لعتبة أعلى بكثير (MEDIA_IMAGE_RELEVANCE_ACCEPT_THRESHOLD).

PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
PIXABAY_IMAGE_SEARCH_URL = "https://pixabay.com/api/"


async def _search_pexels_images(session: aiohttp.ClientSession, keyword: str, per_page: int = _PER_SOURCE_QUOTA):
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        return []
    headers = {"Authorization": api_key}
    params = {"query": keyword, "per_page": per_page, "orientation": "portrait"}
    try:
        async with session.get(PEXELS_PHOTO_SEARCH_URL, headers=headers, params=params, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("Pexels صور [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            results = []
            for photo in data.get("photos", []):
                width, height = photo.get("width"), photo.get("height")
                if not _meets_min_resolution(height) or not _orientation_allowed(width, height):
                    continue
                src = photo.get("src", {})
                url = src.get("large2x") or src.get("original")
                if not url:
                    continue
                results.append({
                    "source": "pexels_photo",
                    "keyword": keyword,
                    "id": str(photo["id"]),
                    "url": url,
                    "media_kind": "image",
                    "width": width,
                    "height": height,
                    "duration": None,
                    "title": None,
                    "description": f"Photo by {photo.get('photographer')}" if photo.get("photographer") else None,
                })
            return results
    except Exception as e:
        logger.warning("خطأ بحث صور Pexels عن '%s': %s", keyword, e)
        return []


async def _search_pixabay_images(session: aiohttp.ClientSession, keyword: str, per_page: int = _PER_SOURCE_QUOTA):
    global _PIXABAY_ENABLED
    if not _PIXABAY_ENABLED:
        logger.info("Pixabay صور [%s]: تخطي (المصدر معطّل حاليًا لهذا التشغيل).", keyword)
        return []
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        logger.warning("PIXABAY_API_KEY غير موجود، تعطيل Pixabay (صور) لهذه الجولة.")
        _PIXABAY_ENABLED = False
        return []
    params = {"key": api_key, "q": keyword, "per_page": per_page, "image_type": "photo"}
    try:
        async with session.get(PIXABAY_IMAGE_SEARCH_URL, params=params, timeout=20) as resp:
            if resp.status in (401, 403, 429):
                logger.warning("Pixabay صور [%s] -> HTTP %s", keyword, resp.status)
                return []
            if resp.status != 200:
                logger.warning("Pixabay صور [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            results = []
            for hit in data.get("hits", []):
                width, height = hit.get("imageWidth"), hit.get("imageHeight")
                if not _meets_min_resolution(height) or not _orientation_allowed(width, height):
                    continue
                url = hit.get("largeImageURL")
                if not url:
                    continue
                results.append({
                    "source": "pixabay_photo",
                    "keyword": keyword,
                    "id": str(hit["id"]),
                    "url": url,
                    "media_kind": "image",
                    "width": width,
                    "height": height,
                    "duration": None,
                    "title": None,
                    "description": f"Tags: {hit.get('tags')}" if hit.get("tags") else None,
                })
            logger.info("Pixabay صور [%s]: %d نتيجة (بعد الفلاتر) من %d.", keyword, len(results), len(data.get("hits", [])))
            return results
    except Exception as e:
        logger.warning("خطأ بحث صور Pixabay عن '%s': %s", keyword, e)
        return []


async def _search_wikimedia_commons_images(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": f"{keyword} filetype:bitmap",
        "srnamespace": 6,
        "srlimit": limit * 3,
        "format": "json",
    }
    try:
        async with session.get(COMMONS_API_URL, params=search_params, headers=COMMONS_HEADERS, timeout=20) as resp:
            if resp.status != 200:
                logger.warning("Wikimedia Commons صور [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            search_results = data.get("query", {}).get("search", [])
    except Exception as e:
        logger.warning("خطأ بحث صور Wikimedia Commons عن '%s': %s", keyword, e)
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
            logger.warning("خطأ جلب imageinfo لـ '%s': %s", title, e)
            continue

        pages = info_data.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        imageinfo_list = page.get("imageinfo") or []
        if not imageinfo_list:
            continue
        imageinfo = imageinfo_list[0]
        mime = imageinfo.get("mime", "")
        if not mime.startswith("image/"):
            continue
        width, height = imageinfo.get("width"), imageinfo.get("height")
        if not _meets_min_resolution(height) or not _orientation_allowed(width, height):
            continue
        file_url = imageinfo.get("url")
        if not file_url:
            continue

        ext_meta = imageinfo.get("extmetadata", {})
        license_url = ext_meta.get("LicenseUrl", {}).get("value", "")
        artist = _strip_html(ext_meta.get("Artist", {}).get("value", ""))
        allowed, needs_attr = _is_license_allowed_for_youtube(license_url, "")
        if not allowed:
            continue
        description = _strip_html(ext_meta.get("ImageDescription", {}).get("value", ""))
        if not _passes_entity_relevance(keyword, title, description):
            logger.info(
                "[تصفية كيان] استُبعدت صورة Wikimedia Commons '%s' لأنها تذكر كيانًا مختلفًا عن '%s'.",
                title, keyword,
            )
            continue

        results.append({
            "source": "wikimedia_commons_photo",
            "keyword": keyword,
            "id": title,
            "url": file_url,
            "media_kind": "image",
            "width": width,
            "height": height,
            "duration": None,
            "requires_attribution": needs_attr,
            "attribution_text": f"\"{title}\" by {artist or 'Wikimedia Commons contributor'} via Wikimedia Commons" if needs_attr else None,
            "title": title,
            "description": description or None,
        })
        if len(results) >= limit:
            break
    return results


async def _search_nasa_images(session: aiohttp.ClientSession, keyword: str, limit: int = _PER_SOURCE_QUOTA):
    global _NASA_ENABLED
    if not _NASA_ENABLED:
        logger.info("NASA صور [%s]: تخطي (المصدر معطّل حاليًا لهذا التشغيل).", keyword)
        return []
    api_key = os.environ.get("Nasa_API_key")
    if not api_key:
        return []
    params = {"q": keyword, "media_type": "image", "api_key": api_key}
    try:
        async with session.get(NASA_SEARCH_URL, params=params, headers=NASA_HEADERS, timeout=15) as resp:
            if resp.status != 200:
                logger.warning("NASA صور [%s] -> HTTP %s", keyword, resp.status)
                return []
            data = await resp.json()
            items = data.get("collection", {}).get("items", [])
    except Exception as e:
        logger.warning("خطأ بحث صور NASA عن '%s': %s", keyword, e)
        return []

    results = []
    for item in items:
        item_data = (item.get("data") or [{}])[0]
        links = item.get("links") or []
        image_link = next(
            (l.get("href") for l in links if l.get("href", "").lower().endswith((".jpg", ".jpeg", ".png"))),
            None,
        )
        if not image_link:
            continue
        title = item_data.get("title", "NASA image")
        description = item_data.get("description") or ""
        keywords_field = " ".join(item_data.get("keywords") or [])
        if not _passes_entity_relevance(keyword, title, description, keywords_field):
            logger.info(
                "[تصفية كيان] استُبعدت صورة NASA '%s' لأنها تذكر كيانًا مختلفًا عن '%s'.",
                title, keyword,
            )
            continue
        results.append({
            "source": "nasa_photo",
            "keyword": keyword,
            "id": item_data.get("nasa_id", title),
            "url": image_link,
            "media_kind": "image",
            "width": None,
            "height": None,
            "duration": None,
            "requires_attribution": True,
            "attribution_text": f"\"{title}\" courtesy of NASA",
            "title": title,
            "description": description,
        })
        if len(results) >= limit:
            break
    logger.info("NASA صور [%s]: %d نتيجة (بعد الفلاتر) من %d.", keyword, len(results), len(items))
    return results


async def search_scene_images(
    visual_keywords: list[str],
    category: str = "",
    topic: str = "",
):
    """يبحث عن صور (بدل فيديو) لنفس الكلمات المفتاحية، عبر نفس المصادر
    ونفس فلاتر الجودة/الرخصة/تعارض الكيانات. يُستدعى فقط من ai_media_verification
    كخطة صارمة بعد فشل كل محاولات الفيديو العادية لمشهد ما."""
    use_nasa = _mentions_space_or_astronomy(category, topic, " ".join(visual_keywords))
    logger.info(
        "خطة الصور: use_nasa=%s (category=%r, topic=%r, keywords=%r), NASA_ENABLED=%s.",
        use_nasa, category, topic, visual_keywords, _NASA_ENABLED,
    )

    async with aiohttp.ClientSession() as session:
        tasks = []
        for kw in visual_keywords:
            tasks.append(_search_pexels_images(session, kw))
            tasks.append(_search_pixabay_images(session, kw))
            tasks.append(_search_wikimedia_commons_images(session, kw))
            if use_nasa and _NASA_ENABLED:
                tasks.append(_search_nasa_images(session, kw))
        results_lists = await asyncio.gather(*tasks)

    total = sum(len(sub) for sub in results_lists)
    logger.info("خطة الصور: إجمالي %d مرشح صورة من كل المصادر لهذه الجولة.", total)
    return [c for sub in results_lists for c in sub]


def search_scene_images_sync(visual_keywords, category="", topic=""):
    return asyncio.run(search_scene_images(visual_keywords, category=category, topic=topic))


async def search_scene_media(
    visual_keywords: list[str],
    youtube_keywords: list[str] | None = None,
    category: str = "",
    topic: str = "",
):
    all_keywords = list(visual_keywords)
    use_nasa = _mentions_space_or_astronomy(category, topic, " ".join(all_keywords))
    logger.info(
        "بحث الفيديو: use_nasa=%s (category=%r, topic=%r, keywords=%r), NASA_ENABLED=%s.",
        use_nasa, category, topic, all_keywords, _NASA_ENABLED,
    )

    nasa_candidates = []
    if use_nasa and _NASA_ENABLED:
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
    if candidate.get("media_kind") == "image":
        # نحدد الامتداد من رابط الصورة نفسه إن أمكن (jpg/png/webp)، وإلا
        # نفترض jpg كامتداد آمن افتراضي (كل المصادر التي أضفناها تعيد صورًا
        # نقطية عادية، لا صيغًا خاصة تحتاج تعاملاً مختلفًا).
        url_no_query = candidate["url"].split("?")[0]
        ext = os.path.splitext(url_no_query)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
    else:
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
