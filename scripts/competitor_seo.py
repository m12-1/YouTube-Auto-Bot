"""
competitor_seo.py
يستخدم مفتاح YouTube منفصل (YOUTUBE_SEARCH_API_KEY) عن مفتاح الرفع، لأن
search.list يستهلك 100 وحدة حصة لكل طلب (مقابل ~1 لعمليات القراءة العادية).
يجلب أفضل 10 نتائج كاملة (عنوان + وصف + وسوم + مشاهدات) لتغذية seo_optimizer.py.
"""
import googleapiclient.discovery
from scripts import config


def get_top_competitors(topic: str, max_results: int = 10) -> list[dict]:
    config.require("YOUTUBE_SEARCH_API_KEY")
    youtube = googleapiclient.discovery.build(
        "youtube", "v3", developerKey=config.YOUTUBE_SEARCH_API_KEY
    )

    search_response = youtube.search().list(
        part="id",
        q=topic,
        type="video",
        order="relevance",
        regionCode=config.CHANNEL_REGION,
        relevanceLanguage="en",
        maxResults=max_results,
    ).execute()  # تكلفة: 100 وحدة — مرة واحدة فقط يومياً لكل موضوع

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    if not video_ids:
        return []

    details_response = youtube.videos().list(
        part="snippet,statistics",
        id=",".join(video_ids),
    ).execute()  # تكلفة: ~1 وحدة لكل فيديو

    results = []
    for item in details_response.get("items", []):
        snippet = item["snippet"]
        results.append({
            "title": snippet["title"],
            "description": snippet.get("description", "")[:500],
            "tags": snippet.get("tags", []),
            "views": int(item.get("statistics", {}).get("viewCount", 0)),
        })
    return results
