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
        f"- {c['title']} (مشاهدات: {c['views']})\n  وصف: {c['description']}"
        for c in competitor_videos
    ) or "لا توجد بيانات منافسين متاحة."

    prompt = f"""
عنوان فيديونا الحالي: "{our_title}"
نص الشرح: "{our_narration}"

فيديوهات منافسة على نفس الموضوع (عناوين وأوصاف):
{competitors_str}

بناءً على ما سبق، ولّد عنوانًا ووصفًا وعلامات (tags) محسّنة لمحرك بحث يوتيوب (SEO)،
بحيث يظهر العنوان بشكل جذاب في المقدمة ولا يكرر عناوين المنافسين حرفيًا.

أعد فقط JSON:
{{
  "optimized_title": "...",
  "optimized_description": "...",
  "tags": ["...", "..."]
}}
"""
    raw = call_gemini_with_rotation(STAGE, [prompt], response_mime_type="application/json")
    return parse_json_response(raw)


if __name__ == "__main__":
    vids = fetch_competitor_videos(["black holes facts", "space shorts"])
    print(optimize_seo("حقيقة مذهلة عن الثقوب السوداء", "الثقوب السوداء...", vids))
