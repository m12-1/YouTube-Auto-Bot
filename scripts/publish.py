"""
publish.py
يرفع الفيديو النهائي (بعد التأكد من دقة 1080p على الأقل من Remotion output)
عبر YouTube Data API باستخدام OAuth Refresh Token (مطلوب لـ videos.insert،
مفتاح API البسيط لا يكفي لعمليات الكتابة).
"""
import google.oauth2.credentials
import googleapiclient.discovery
from googleapiclient.http import MediaFileUpload

from scripts import config
from scripts.telegram_alerts import send_alert, alert_step_failed


def _get_authenticated_service():
    config.require(
        "YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET", "YOUTUBE_OAUTH_REFRESH_TOKEN"
    )
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=config.YOUTUBE_OAUTH_REFRESH_TOKEN,
        client_id=config.YOUTUBE_OAUTH_CLIENT_ID,
        client_secret=config.YOUTUBE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)


def _verify_1080p(video_path: str):
    """تحقق سريع من دقة الفيديو قبل الرفع باستخدام ffprobe — يمنع رفع ملف بدقة أقل بالخطأ."""
    import subprocess
    import json as _json
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
        capture_output=True, text=True,
    )
    info = _json.loads(result.stdout)
    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    width, height = video_stream["width"], video_stream["height"]
    # للفيديو العمودي (شورت) العرض هو البُعد الحرج (1080x1920)، وللأفقي الارتفاع
    # (1920x1080) — نفحص البُعد الأصغر مطلقاً حتى يغطي الحالتين بقاعدة واحدة
    smaller_dimension = min(width, height)
    if smaller_dimension < config.MIN_ALLOWED_RESOLUTION:
        raise ValueError(
            f"الفيديو {video_path} بدقة {width}x{height} — أقل من الحد الأدنى "
            f"{config.MIN_ALLOWED_RESOLUTION}p المطلوب!"
        )
    return width, height


def upload_video(video_path: str, title: str, description: str, tags: list[str],
                  thumbnail_path: str = None, is_short: bool = False) -> str:
    width, height = _verify_1080p(video_path)
    print(f"تأكيد الدقة: {width}x{height} ✅")

    youtube = _get_authenticated_service()

    final_title = title if not is_short else f"{title} #shorts"
    body = {
        "snippet": {
            "title": final_title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "27",  # Education
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response["id"]

    if thumbnail_path:
        youtube.thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(thumbnail_path)
        ).execute()

    send_alert(f"تم نشر الفيديو بنجاح: https://youtu.be/{video_id}", level="info")
    return video_id


def publish_pair(long_video_path, long_meta, long_thumbnail,
                  short_video_path, short_meta):
    """ينشر الفيديو الطويل والشورت بفارق ساعات (يُنفَّذ عبر جدولة GitHub Actions منفصلة
    لتفادي مظهر spam من نشرين متتاليين بنفس اللحظة)."""
    try:
        long_id = upload_video(
            long_video_path, long_meta["title"], long_meta["description"],
            long_meta["tags"], thumbnail_path=long_thumbnail, is_short=False,
        )
        return long_id
    except Exception as e:
        alert_step_failed("publish long video", e)
        raise
