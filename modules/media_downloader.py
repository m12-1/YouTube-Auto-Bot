"""
المرحلة 4: البحث المتوازي عن الوسائط في Pexels + Pixabay لكل مشهد،
باستخدام visual_keywords (من المرحلة 3) مع youtube_keywords كبدائل احتياطية.
"""

import os
import asyncio
import logging
import aiohttp

logger = logging.getLogger("modules.media_downloader")

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/videos/"


async def _search_pexels(session: aiohttp.ClientSession, keyword: str, per_page: int = 5):
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
                if files:
                    results.append({
                        "source": "pexels",
                        "keyword": keyword,
                        "id": str(video["id"]),
                        "url": files[0]["link"],
                        "duration": video.get("duration"),
                    })
            return results
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث Pexels عن '%s': %s", keyword, e)
        return []


async def _search_pixabay(session: aiohttp.ClientSession, keyword: str, per_page: int = 5):
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
                best = videos.get("large") or videos.get("medium") or videos.get("small")
                if best:
                    results.append({
                        "source": "pixabay",
                        "keyword": keyword,
                        "id": str(hit["id"]),
                        "url": best["url"],
                        "duration": hit.get("duration"),
                    })
            return results
    except Exception as e:  # noqa: BLE001
        logger.warning("خطأ بحث Pixabay عن '%s': %s", keyword, e)
        return []


async def search_scene_media(visual_keywords: list[str], youtube_keywords: list[str] | None = None):
    """
    يبحث بالتوازي في Pexels وPixabay باستخدام كل الكلمات المفتاحية للمشهد،
    ويعيد قائمة موحّدة من المرشحين (بدون تحميل الملفات بعد).
    """
    all_keywords = list(visual_keywords) + list(youtube_keywords or [])
    async with aiohttp.ClientSession() as session:
        tasks = []
        for kw in all_keywords:
            tasks.append(_search_pexels(session, kw))
            tasks.append(_search_pixabay(session, kw))
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

    async with aiohttp.ClientSession() as session:
        async with session.get(candidate["url"], timeout=60) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
    return path


def search_scene_media_sync(visual_keywords, youtube_keywords=None):
    return asyncio.run(search_scene_media(visual_keywords, youtube_keywords))


if __name__ == "__main__":
    print(search_scene_media_sync(["black hole space", "galaxy stars"]))
