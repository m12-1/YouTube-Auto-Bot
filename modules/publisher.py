"""
المرحلة 9: النشر على يوتيوب.
يستخدم refresh_token فعليًا لتجديد access_token تلقائيًا في كل تشغيل
(YOUTUBE_OAUTH_CLIENT_ID / SECRET / REFRESH_TOKEN)، بدون أي تدخل يدوي.
"""

import os
import logging

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger("modules.publisher")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_youtube_client():
    required = ["YOUTUBE_OAUTH_REFRESH_TOKEN", "YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"أسرار يوتيوب ناقصة: {', '.join(missing)}")

    # لا نمرّر scopes هنا عمدًا: عملية تجديد access_token عبر refresh_token
    # لا تحتاج لإرسال scope أصلًا، وإرساله قد يسبب invalid_scope من طرف
    # Google إن لم يطابق حرفيًا (ترتيبًا وصياغةً) النطاق الذي صدر به
    # refresh_token أصلًا وقت الموافقة الأولى. تمرير None هنا آمن تمامًا
    # لأن creds.refresh() يعتمد فقط على refresh_token/client_id/client_secret،
    # ولا يُستخدم SCOPES إلا في تدفق الموافقة الأولية (خارج هذا الملف).
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=None,
    )

    # يجبر التجديد الفعلي عبر refresh_token في كل مرة (بدل الاعتماد على creds.valid فقط)
    try:
        creds.refresh(Request())
        logger.info("تم تجديد YouTube access token بنجاح عبر refresh_token.")
    except Exception as e:
        logger.error("فشل تجديد access token: %s", e)
        raise

    return build("youtube", "v3", credentials=creds)


def publish_video(video_path: str, title: str, description: str, tags: list[str],
                   category_id: str = "27", privacy_status: str = "public") -> str:
    """يرفع الفيديو ويعيد video_id."""
    youtube = get_youtube_client()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("رفع %d%%", int(status.progress() * 100))

    video_id = response["id"]
    logger.info("تم النشر بنجاح: https://youtube.com/watch?v=%s", video_id)
    return video_id
