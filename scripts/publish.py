"""
publish.py
يرفع الفيديو النهائي (بعد التأكد من دقة 1080p على الأقل من Remotion output)
عبر YouTube Data API باستخدام OAuth Refresh Token.
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
    # إصلاح خطأ invalid_scope: ترك الصلاحية مرنة لتتطابق مع الـ Refresh Token المولد تلقائياً
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=config.YOUTUBE_OAUTH_REFRESH_TOKEN,
        client_id=config.YOUTUBE_OAUTH_CLIENT_ID,
        client_secret=config.YOUTUBE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)


def _verify_1080p(video_path: str):
    """تحقق سريع من دقة الفيديو قبل الرفع باستخدام ffprobe."""
    if not video_path:
        raise ValueError(
            "مسار الفيديو فارغ (None) — لا يمكن فحص الدقة أو الرفع. "
            "هذا يعني إن render_video_via_remotion فشلت أو ما تم استدعاؤها أصلاً."
        )
    import subprocess
    import json as _json
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
        capture_output=True, text=True,
    )
    info = _json.loads(result.stdout)
    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    width, height = video_stream["width"], video_stream["height"]
    smaller_dimension = min(width, height)
    if smaller_dimension < config.MIN_ALLOWED_RESOLUTION:
        raise ValueError(
            "الفيديو {video_path} بدقة {width}x{height} — أقل من الحد الأدنى "
            "{config.MIN_ALLOWED_RESOLUTION}p المطلوب!"
        )
    return width, height


def upload_video(video_path: str, title: str, description: str, tags: list[str],
                 thumbnail_path: str = None, is_short: bool = False) -> str:
    width, height = _verify_1080p(video_path)
    print(f"تأكيد الدقة: {width}x{height} ✅")

    youtube = _get_authenticated_service()

    final_title = title if not is_short else f"{title} #shorts"
    privacy = "private" if config.TEST_MODE else "public"
    print(f"[PUBLISH] وضع الخصوصية: {privacy} (TEST_MODE={config.TEST_MODE})")

    body = {
        "snippet": {
            "title": final_title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "27",  # Education
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response["id"]

    if thumbnail_path:
        try:
            youtube.thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(thumbnail_path)
            ).execute()
        except Exception as th_err:
            print(f"[THUMBNAIL UPLOAD WARNING] تعذر رفع الغلاف البديل، سيتم ترك يوتيوب يختار غلافاً تلقائياً. السبب: {th_err}")

    send_alert(f"تم نشر الفيديو بنجاح: https://youtu.be/{video_id}", level="info")
    return video_id


def publish_pair(long_video_path=None, long_meta=None, long_thumbnail=None,
                 short_video_path=None, short_meta=None, short_thumbnail=None):
    results = {}
    try:
        if long_video_path:
            results["long_id"] = upload_video(
                long_video_path, long_meta["title"], long_meta["description"],
                long_meta["tags"], thumbnail_path=long_thumbnail, is_short=False,
            )
        if short_video_path:
            results["short_id"] = upload_video(
                short_video_path, short_meta["title"], short_meta["description"],
                short_meta["tags"], thumbnail_path=short_thumbnail, is_short=True,
            )
        if not results:
            raise ValueError("لم يُمرَّر أي مسار فيديو صالح (طويل أو شورت) لـ publish_pair")
        return results
    except Exception as e:
        alert_step_failed("publish", e)
        raise
