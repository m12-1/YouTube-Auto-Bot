"""
trend_scanner.py
يعمل كل 6 ساعات (عبر GitHub Actions cron).
1. يسحب أفضل 50 فيديو رائج بأمريكا عبر YouTube Data API الرسمي (videos.list — استهلاك خفيف).
2. يستبعد الفئات/الكلمات الحساسة.
3. يفحص التكرار مقابل Trend_Log عبر embeddings (آخر 60 يوم).
4. يرسل القائمة المتبقية لـ Gemini (نموذج خفيف) ليختار الأنسب اليوم مع تبرير.
"""
import os
import json
import re
import numpy as np
import googleapiclient.discovery

from scripts import config, gemini_client, sheets_client
from scripts.telegram_alerts import send_alert, alert_step_failed
from scripts.embeddings_dedup import is_duplicate_topic
from scripts.content_policy import contains_blocked_content

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")


def fetch_trending_videos(max_results: int = 50) -> list[dict]:
    config.require("YOUTUBE_SEARCH_API_KEY")
    youtube = googleapiclient.discovery.build(
        "youtube", "v3", developerKey=config.YOUTUBE_SEARCH_API_KEY
    )
    request = youtube.videos().list(
        part="snippet,statistics",
        chart="mostPopular",
        regionCode=config.CHANNEL_REGION,
        maxResults=max_results,
    )
    response = request.execute()
    videos = []
    for item in response.get("items", []):
        snippet = item["snippet"]
        videos.append({
            "video_id": item["id"],
            "title": snippet["title"],
            "description": snippet.get("description", "")[:300],
            "category_id": snippet.get("categoryId"),
            "tags": snippet.get("tags", []),
            "views": int(item.get("statistics", {}).get("viewCount", 0)),
        })
    return videos


def is_blocked(video: dict) -> bool:
    if video["category_id"] in config.BLOCKED_CATEGORY_IDS:
        return True
    text = video["title"] + " " + video["description"]
    blocked, category = contains_blocked_content(text)
    if blocked:
        print(f"[BLOCKED] '{video['title']}' — الفئة: {category}")
    return blocked


def filter_candidates(videos: list[dict]) -> list[dict]:
    safe = [v for v in videos if not is_blocked(v)]
    non_duplicate = [v for v in safe if not is_duplicate_topic(v["title"], SPREADSHEET_ID)]
    return non_duplicate


def select_best_topic(candidates: list[dict]) -> dict:
    titles_list = "\n".join(f"- {v['title']}" for v in candidates[:20])
    prompt = f"""
أنت باحث محتوى لقناة يوتيوب أمريكية موجهة لمحتوى معلوماتي مشوّق (غير إخباري/غير سياسي).
هذه قائمة مواضيع رائجة اليوم:
{titles_list}

اختر موضوعاً واحداً فقط هو الأنسب لصناعة فيديو معلوماتي مشوّق (غير مرتبط بأخبار عاجلة أو أشخاص مثيرين للجدل).

مهم جداً بخصوص "core_topic": يجب أن يكون عبارة قصيرة جداً (كلمتين إلى 4 كلمات
كحد أقصى) تصف الموضوع الأساسي فقط (مثال: "video game psychology" أو "ocean
exploration") — بدون أي جملة كاملة، بدون تعليق أو تحليل أو أسباب أو علامات
ترقيم مثل الفاصلة. أي شرح أو تبرير يذهب حصراً في حقل "reason"، وليس بـ "core_topic".

أجب بصيغة JSON فقط بهذا الشكل:
{{"chosen_title": "...", "core_topic": "...", "reason": "..."}}
"""
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_TREND_FILTER, key_type="light", json_mode=True
    )
    result = json.loads(raw)

    # طبقة حماية إضافية: حتى لو تجاهل النموذج التعليمات وأرجع جملة طويلة بدل
    # عبارة قصيرة بـ core_topic (يحدث أحياناً)، نقصّها هنا لأول 4 كلمات ونزيل
    # علامات الترقيم الزائدة، بدل تسريب نص مثل "This topic is evergreen,"
    # لاحقاً إلى كلمات بحث الفيديوهات (Pixabay/Pexels) ويفسدها.
    core_topic = result.get("core_topic", "")
    cleaned = re.sub(r"[,.;:!?]", "", core_topic).strip()
    result["core_topic"] = " ".join(cleaned.split()[:4])
    return result


def run():
    if not sheets_client.is_system_enabled(SPREADSHEET_ID):
        print("النظام متوقف عبر System_Control. تخطي.")
        return

    try:
        videos = fetch_trending_videos()
        candidates = filter_candidates(videos)
        if not candidates:
            send_alert("لا توجد مواضيع ترند آمنة اليوم بعد الفلترة.", level="warning")
            return

        chosen = select_best_topic(candidates)
        sheets_client.append_row(
            SPREADSHEET_ID, config.Paths().sheets_trend_log,
            [chosen["chosen_title"], chosen["core_topic"], chosen["reason"]],
        )
        print(f"تم اختيار الموضوع: {chosen['chosen_title']}")

    except Exception as e:
        alert_step_failed("trend_scanner", e)
        raise


if __name__ == "__main__":
    run()
