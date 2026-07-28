"""
المرحلة 8: تحسين SEO بمقارنة المنافسين.
- يبحث في يوتيوب (YOUTUBE_SEARCH_API_KEY) عن أعلى فيديوهات لنفس الكلمات المفتاحية
  (ترتيب: الأحدث نشرًا + الأعلى مشاهدات ضمن ذلك).
- يرسل عناوينها/أوصافها إلى Gemini (GEMINI_KEY_IMAGE) مع عنوان فيديونا لتوليد
  عنوان ووصف وعلامات (tags) محسّنة للسيو.
"""

import os
import logging

import requests

from shared.gemini_client import call_gemini_with_rotation, parse_json_response
from config import TOP_COMPETITOR_VIDEOS

logger = logging.getLogger("modules.competitor_seo_optimizer")

STAGE = "competitor_seo_optimizer"

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def fetch_competitor_videos(youtube_keywords: list[str], max_results: int = TOP_COMPETITOR_VIDEOS) -> list[dict]:
    api_key = os.environ.get("YOUTUBE_SEARCH_API_KEY")
    if not api_key:
        logger.warning("YOUTUBE_SEARCH_API_KEY غير موجود، تخطي مقارنة المنافسين.")
        return []

    query = " ".join(youtube_keywords[:5])
    search_params = {
        "key": api_key, "q": query, "part": "snippet", "type": "video",
        "order": "date", "maxResults": max_results * 2,  # نجلب أكثر ثم نرتب بالمشاهدات
        # نفرض لغة إنجليزية على نتائج المنافسين حتى لا تُجلب فيديوهات
        # بلغة مختلفة (عربية مثلاً) تُضلّل Gemini لاحقًا عن لغة العنوان/الوصف
        # المطلوبة فعليًا لجمهورنا الإنجليزي.
        "relevanceLanguage": "en",
    }
    resp = requests.get(YOUTUBE_SEARCH_URL, params=search_params, timeout=20)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    video_ids = [it["id"]["videoId"] for it in items if "videoId" in it.get("id", {})]
    if not video_ids:
        return []

    stats_params = {"key": api_key, "id": ",".join(video_ids), "part": "snippet,statistics"}
    stats_resp = requests.get(YOUTUBE_VIDEOS_URL, params=stats_params, timeout=20)
    stats_resp.raise_for_status()
    videos = stats_resp.json().get("items", [])

    videos.sort(key=lambda v: int(v.get("statistics", {}).get("viewCount", 0)), reverse=True)
    top = videos[:max_results]

    return [
        {
            "title": v["snippet"]["title"],
            "description": v["snippet"].get("description", "")[:300],
            "views": v.get("statistics", {}).get("viewCount", "0"),
            "published_at": v["snippet"].get("publishedAt"),
        }
        for v in top
    ]


def optimize_seo(our_title: str, our_narration: str, competitor_videos: list[dict]) -> dict:
    competitors_str = "\n".join(
        f"- {c['title']} (views: {c['views']})\n  description: {c['description']}"
        for c in competitor_videos
    ) or "No competitor data available."

    prompt = f"""
Our current video title: "{our_title}"
Narration script: "{our_narration}"

Competing videos on the same topic (titles and descriptions):
{competitors_str}

Based on the above, generate an SEO-optimized title, description, and tags for YouTube search,
with a catchy title upfront that does not copy competitor titles verbatim.

CRITICAL: the output MUST be in English, regardless of the language of any competitor
titles/descriptions shown above. Never output Arabic or any other language.

Return only JSON:
{{
  "optimized_title": "...",
  "optimized_description": "...",
  "tags": ["...", "..."]
}}
"""
    raw = call_gemini_with_rotation(STAGE, [prompt], response_mime_type="application/json")
    seo = parse_json_response(raw)

    # فرض حدود YouTube Data API الفعلية كي لا يفشل الرفع في آخر مرحلة
    seo["optimized_title"] = (seo.get("optimized_title") or "")[:100]
    seo["optimized_description"] = (seo.get("optimized_description") or "")[:5000]
    tags, total = [], 0
    for t in seo.get("tags") or []:
        if total + len(t) > 500:
            break
        tags.append(t)
        total += len(t)
    seo["tags"] = tags

    return seo


if __name__ == "__main__":
    vids = fetch_competitor_videos(["black holes facts", "space shorts"])
    print(optimize_seo("حقيقة مذهلة عن الثقوب السوداء", "الثقوب السوداء...", vids))
