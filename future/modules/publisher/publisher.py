"""
Publisher module.
Handles publishing videos to YouTube using the Data API v3.
"""
from typing import Dict, Any
import datetime

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
import config.settings
import config.pipeline_config

MODULE_NAME = "publisher"
logger = get_logger(__name__)

# Note: google-api-python-client is required, but we must fail gracefully if not available.
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials
    HAS_GOOGLE_API = True
except ImportError:
    HAS_GOOGLE_API = False

@retry(max_attempts=3, base_delay_seconds=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Publish a video to YouTube.
    """
    logger.info(f"[{MODULE_NAME}] Starting publisher")
    
    try:
        require_keys(input_json, ["run_id", "topic"])
        
        run_id = input_json.get("run_id")
        topic = input_json.get("topic")
        
        seo = input_json.get("seo", {})
        video_path = input_json.get("video_output_path")
        thumbnail_path = input_json.get("thumbnail_path")
        privacy = input_json.get("privacy", "private")
        playlist_id = input_json.get("playlist_id")
        scheduled_time = input_json.get("scheduled_publish_time")
        
        client_id = getattr(config.settings, "YOUTUBE_OAUTH_CLIENT_ID", None)
        client_secret = getattr(config.settings, "YOUTUBE_OAUTH_CLIENT_SECRET", None)
        refresh_token = getattr(config.settings, "YOUTUBE_OAUTH_REFRESH_TOKEN", None)
        
        if not HAS_GOOGLE_API or not all([client_id, client_secret, refresh_token]) or not video_path:
            logger.warning(f"[{MODULE_NAME}] Google API client unavailable, missing credentials, or missing video path. Falling back to simulated upload.")
            return build_response(
                module=MODULE_NAME,
                status="success",
                data={
                    "video_id": f"simulated_{run_id}",
                    "video_url": f"https://youtube.com/watch?v=simulated_{run_id}",
                    "publish_time": scheduled_time or datetime.datetime.now().isoformat(),
                    "upload_status": "simulated",
                    "playlist_id": playlist_id,
                    "rendered": False
                }
            )
        
        credentials = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        
        youtube = build("youtube", "v3", credentials=credentials)
        
        body = {
            "snippet": {
                "title": seo.get("title", f"Generated Video about {topic}"),
                "description": seo.get("description", f"A short video about {topic}"),
                "tags": seo.get("tags", []),
                "categoryId": "22"
            },
            "status": {
                "privacyStatus": privacy
            }
        }
        
        if scheduled_time:
            body["status"]["publishAt"] = scheduled_time
            body["status"]["privacyStatus"] = "private"
        
        logger.info(f"[{MODULE_NAME}] Initiating media file upload from {video_path}")
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"[{MODULE_NAME}] Upload progress: {int(status.progress() * 100)}%")
                
        video_id = response.get("id")
        logger.info(f"[{MODULE_NAME}] Upload complete. Video ID: {video_id}")
        
        if thumbnail_path:
            logger.info(f"[{MODULE_NAME}] Uploading thumbnail from {thumbnail_path}")
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path)
            ).execute()
            
        if playlist_id:
            logger.info(f"[{MODULE_NAME}] Adding to playlist {playlist_id}")
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id
                        }
                    }
                }
            ).execute()
        
        return build_response(
            module=MODULE_NAME,
            status="success",
            data={
                "video_id": video_id,
                "video_url": f"https://youtube.com/watch?v={video_id}",
                "publish_time": scheduled_time or datetime.datetime.now().isoformat(),
                "upload_status": "published",
                "playlist_id": playlist_id,
                "rendered": True
            }
        )
        
    except ContractError as e:
        logger.error(f"[{MODULE_NAME}] Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.exception(f"[{MODULE_NAME}] Upload failed: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
