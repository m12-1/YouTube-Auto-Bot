"""
modules/media_downloader/media_downloader.py

Receives a media plan and downloads matching stock video/image
candidates from Pexels and/or Pixabay, with on-disk caching, retries,
and progress logging.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "media_plan": [
            {
                "scene_id": str, "media_type": "video" | "image",
                "duration_seconds": float, "priority": str,
                "search_keywords": [str, ...], "alternative_keywords": [str, ...],
                "camera_movement": str
            },
            ...
        ]
    }

input_json (optional keys):
    {
        "candidates_per_scene": int   # defaults to 3
    }

output_json:
    {
        "status": "success" | "error",
        "module": "media_downloader",
        "data": {
            "run_id": str,
            "topic": str,
            "downloads": [
                {
                    "scene_id": str,
                    "provider": "pexels" | "pixabay" | "unavailable",
                    "candidates": [
                        {
                            "candidate_id": str,
                            "url": str,
                            "local_path": str,
                            "width": int,
                            "height": int,
                            "duration_seconds": float | null,
                            "cached": bool
                        },
                        ...
                    ]
                },
                ...
            ]
        },
        "error": str | null
    }
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import pipeline_config, settings
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry
from shared.path_utils import safe_path, sanitize_filename, PROJECT_ROOT

logger = get_logger(__name__)

MODULE_NAME = "media_downloader"

_PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
_PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
_PIXABAY_SEARCH_URL = "https://pixabay.com/api/"
_PIXABAY_VIDEO_SEARCH_URL = "https://pixabay.com/api/videos/"

DEFAULT_CANDIDATES_PER_SCENE = 3


def _cache_path(provider: str, media_type: str, query: str, candidate_id: str) -> str:
    """
    Build a stable local cache path for a given candidate, so repeated
    downloads of the same exact candidate reuse the cached file.

    IMPORTANT: this is keyed by candidate_id (not just provider/media_type/
    query). Earlier this only hashed provider+media_type+query, so every
    distinct candidate returned for the same search query collapsed onto
    the *same* cache file -- the first candidate's bytes got silently
    reused (or overwritten) for every other candidate "downloaded" for
    that query, even though each had its own id/url. That meant multiple
    "different" candidates for a scene could all actually be the same
    physical file on disk, defeating quality filtering/ranking picking
    between genuinely different options.

    Args:
        provider: "pexels" or "pixabay".
        media_type: "video" or "image".
        query: The search keyword string.
        candidate_id: The provider's unique id for this specific candidate.

    Returns:
        A local filesystem path under `config.pipeline_config.MEDIA_CACHE_DIR`.
    """
    digest = hashlib.sha256(
        f"{provider}:{media_type}:{query}:{candidate_id}".encode("utf-8")
    ).hexdigest()[:16]
    extension = "mp4" if media_type == "video" else "jpg"
    safe_provider = sanitize_filename(provider)
    target_path = safe_path(pipeline_config.MEDIA_CACHE_DIR, safe_provider, f"{digest}.{extension}")
    return str(target_path)


@retry(
    max_attempts=pipeline_config.MEDIA_DOWNLOAD_MAX_ATTEMPTS,
    exceptions=(requests.RequestException,),
)
def _search_pexels(query: str, media_type: str, per_page: int) -> List[Dict[str, Any]]:
    """
    Search Pexels for candidate media matching a query.

    Args:
        query: Search keyword.
        media_type: "video" or "image".
        per_page: Number of results to request.

    Returns:
        A list of raw candidate dicts (provider-specific shape).

    Raises:
        requests.RequestException: On network failure (retried by decorator).
    """
    if not settings.PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY not configured.")

    url = _PEXELS_VIDEO_SEARCH_URL if media_type == "video" else _PEXELS_SEARCH_URL
    headers = {"Authorization": settings.PEXELS_API_KEY}
    params = {"query": query, "per_page": per_page, "orientation": "portrait"}

    response = requests.get(
        url, headers=headers, params=params, timeout=pipeline_config.MEDIA_DOWNLOAD_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    body = response.json()
    return body.get("videos", []) if media_type == "video" else body.get("photos", [])


@retry(
    max_attempts=pipeline_config.MEDIA_DOWNLOAD_MAX_ATTEMPTS,
    exceptions=(requests.RequestException,),
)
def _search_pixabay(query: str, media_type: str, per_page: int) -> List[Dict[str, Any]]:
    """
    Search Pixabay for candidate media matching a query.

    Args:
        query: Search keyword.
        media_type: "video" or "image".
        per_page: Number of results to request.

    Returns:
        A list of raw candidate dicts (provider-specific shape).

    Raises:
        requests.RequestException: On network failure (retried by decorator).
    """
    if not settings.PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY not configured.")

    url = _PIXABAY_VIDEO_SEARCH_URL if media_type == "video" else _PIXABAY_SEARCH_URL

    # IMPORTANT: Pixabay's Video API has no "orientation" parameter -- that
    # option only exists on their Image API. Sending it here does nothing;
    # Pixabay silently ignores it and returns videos in whatever aspect
    # ratio matched the query (usually landscape), which then get rejected
    # almost entirely by media_quality_filter's portrait check downstream.
    # This was previously the main reason Pixabay attempts returned far
    # fewer usable candidates than Pexels (which DOES support orientation
    # on its video endpoint). Since Pixabay can't filter server-side for
    # video, request a much larger pool so the few portrait videos that do
    # exist for a query are more likely to be among the results, and let
    # media_quality_filter's orientation check do the real filtering.
    if media_type == "video":
        params = {
            "key": settings.PIXABAY_API_KEY,
            "q": query,
            "per_page": max(per_page * 4, 50),
        }
    else:
        # Image API DOES support orientation -- keep using it there.
        params = {
            "key": settings.PIXABAY_API_KEY,
            "q": query,
            "per_page": max(per_page, 3),
            "orientation": "vertical",
        }

    response = requests.get(
        url, params=params, timeout=pipeline_config.MEDIA_DOWNLOAD_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    body = response.json()
    return body.get("hits", [])


@retry(
    max_attempts=pipeline_config.MEDIA_DOWNLOAD_MAX_ATTEMPTS,
    exceptions=(requests.RequestException,),
)
def _download_file(url: str, local_path: str) -> None:
    """
    Download a remote file to a local path, creating parent directories
    as needed.

    Args:
        url: Remote file URL.
        local_path: Destination path on disk.

    Raises:
        requests.RequestException: On network failure (retried by decorator).
    """
    local_p = Path(local_path)
    local_p.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, stream=True, timeout=pipeline_config.MEDIA_DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    with open(local_path, "wb") as handle:
        for chunk in response.iter_content(chunk_size=8192):
            handle.write(chunk)


def _normalize_pexels_candidate(raw: Dict[str, Any], media_type: str) -> Dict[str, Any]:
    """
    Normalize a raw Pexels result into the module's candidate shape.

    IMPORTANT: "thumbnail_url" is always a JPEG/PNG image, never the .mp4
    itself. ai_media_verification's Gemini vision call needs a real image
    to attach as inline_data -- it cannot decode a video file, so for video
    candidates it must use this thumbnail instead of "url" (the video
    file). Without this field, "vision" verification for every video
    candidate had nothing valid to actually look at.
    """
    if media_type == "video":
        video_files = sorted(
            raw.get("video_files", []), key=lambda f: f.get("width", 0), reverse=True
        )
        best = video_files[0] if video_files else {}
        return {
            "candidate_id": str(raw.get("id")),
            "url": best.get("link", ""),
            # Pexels' Video Search response includes a top-level "image"
            # field: a JPEG thumbnail frame for the video.
            "thumbnail_url": raw.get("image", ""),
            "width": best.get("width", 0),
            "height": best.get("height", 0),
            "duration_seconds": raw.get("duration"),
            "source_text": _raw_candidate_text(raw, "pexels"),
        }
    image_url = raw.get("src", {}).get("original", "")
    return {
        "candidate_id": str(raw.get("id")),
        "url": image_url,
        # For still images, the full-size image itself doubles as the
        # thumbnail used for vision verification.
        "thumbnail_url": raw.get("src", {}).get("medium", "") or image_url,
        "width": raw.get("width", 0),
        "height": raw.get("height", 0),
        "duration_seconds": None,
        "source_text": _raw_candidate_text(raw, "pexels"),
    }


def _normalize_pixabay_candidate(raw: Dict[str, Any], media_type: str) -> Dict[str, Any]:
    """
    Normalize a raw Pixabay result into the module's candidate shape.

    IMPORTANT: "thumbnail_url" is always a JPEG/PNG image, never the video
    file. See the matching note on `_normalize_pexels_candidate` -- this is
    what ai_media_verification's Gemini vision call actually inspects.
    """
    if media_type == "video":
        videos = raw.get("videos", {})
        best = videos.get("large") or videos.get("medium") or {}
        picture_id = raw.get("picture_id", "")
        return {
            "candidate_id": str(raw.get("id")),
            "url": best.get("url", ""),
            # Pixabay's Video API doesn't return a direct thumbnail URL,
            # but documents this convention for deriving one from
            # "picture_id" (the same id used on the Pixabay video page).
            "thumbnail_url": (
                f"https://i.vimeocdn.com/video/{picture_id}_640x360.jpg" if picture_id else ""
            ),
            "width": best.get("width", 0),
            "height": best.get("height", 0),
            "duration_seconds": raw.get("duration"),
            "source_text": _raw_candidate_text(raw, "pixabay"),
        }
    image_url = raw.get("largeImageURL", "")
    return {
        "candidate_id": str(raw.get("id")),
        "url": image_url,
        # webformatURL is a smaller resized version of the same image --
        # cheaper to fetch for vision verification than the full-size one.
        "thumbnail_url": raw.get("webformatURL", "") or image_url,
        "width": raw.get("imageWidth", 0),
        "height": raw.get("imageHeight", 0),
        "duration_seconds": None,
        "source_text": _raw_candidate_text(raw, "pixabay"),
    }


def _raw_candidate_text(raw: Dict[str, Any], provider: str) -> str:
    """
    Extract whatever free text a provider gives us about a raw result
    (tags, alt text, url slug, uploader-written title) so it can be
    checked against negative keywords before we ever download it.
    """
    if provider == "pixabay":
        return f"{raw.get('tags', '')}".lower()
    # Pexels doesn't expose tags; use alt text / photographer-provided
    # description and the URL slug (Pexels URLs embed a short description).
    return f"{raw.get('alt', '')} {raw.get('url', '')}".lower()


def _matches_negative_keywords(text: str, negative_keywords: List[str]) -> Optional[str]:
    """
    Return the first negative keyword found in `text`, or None if the
    text is clean. Cheap string containment is intentional: this is a
    pre-Gemini filter meant to reject obviously-wrong results (e.g. a
    "podcast"/"office" clip for a black-hole scene) before spending any
    AI verification budget on them.
    """
    for negative in negative_keywords:
        needle = negative.strip().lower()
        if needle and needle in text:
            return needle
    return None


def _fetch_candidates_for_scene(
    keywords: List[str],
    media_type: str,
    candidates_per_scene: int,
    negative_keywords: Optional[List[str]] = None,
    providers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Try each configured provider, in priority order, for the given
    keywords until candidates are found.

    Args:
        keywords: Ordered list of search keywords to try (primary first).
        media_type: "video" or "image".
        candidates_per_scene: How many candidates to request.
        providers: Optional override of which providers to try, and in
            what order. Used by the media engine's Fallback/re-search
            loop (core.pipeline_controller) to force a single, specific
            provider on a given retry attempt instead of always
            restarting from `MEDIA_PROVIDER_PRIORITY[0]`. Defaults to
            `pipeline_config.MEDIA_PROVIDER_PRIORITY` when omitted.

    Returns:
        A dict with "provider" and "raw_candidates" (may be empty).
    """
    negative_keywords = negative_keywords or []
    provider_order = providers or pipeline_config.MEDIA_PROVIDER_PRIORITY

    for provider in provider_order:
        # Ask for more than needed since the negative-keyword filter
        # below will drop some results before we get to candidates_per_scene.
        fetch_count = max(candidates_per_scene * 3, candidates_per_scene)
        for query in keywords:
            try:
                if provider == "pexels":
                    raw = _search_pexels(query, media_type, fetch_count)
                elif provider == "pixabay":
                    raw = _search_pixabay(query, media_type, fetch_count)
                else:
                    continue

                if not raw:
                    continue

                clean_raw = []
                for item in raw:
                    hit = _matches_negative_keywords(_raw_candidate_text(item, provider), negative_keywords)
                    if hit:
                        logger.info(
                            "Media Search rejected candidate for query='%s' "
                            "(matched negative keyword '%s')",
                            query,
                            hit,
                        )
                        continue
                    clean_raw.append(item)

                if clean_raw:
                    return {"provider": provider, "raw_candidates": clean_raw, "query": query}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Media search failed for provider=%s query='%s': %s", provider, query, exc
                )
                continue

    return {"provider": "unavailable", "raw_candidates": [], "query": keywords[0] if keywords else ""}


def _download_scene_media(
    scene_id: str,
    media_type: str,
    keywords: List[str],
    candidates_per_scene: int,
    negative_keywords: Optional[List[str]] = None,
    providers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Fetch, normalize, and download candidate media for a single scene.

    Args:
        scene_id: The storyboard scene identifier.
        media_type: "video" or "image".
        keywords: Search keywords (primary + alternatives) to try.
        candidates_per_scene: How many candidates to request/return.
        providers: Optional provider override (see `_fetch_candidates_for_scene`).

    Returns:
        A dict with "scene_id", "provider", and "candidates".
    """
    search_result = _fetch_candidates_for_scene(
        keywords, media_type, candidates_per_scene, negative_keywords, providers
    )
    provider = search_result["provider"]
    raw_candidates = search_result["raw_candidates"]
    query = search_result["query"]

    candidates: List[Dict[str, Any]] = []

    for raw in raw_candidates[:candidates_per_scene]:
        normalized = (
            _normalize_pexels_candidate(raw, media_type)
            if provider == "pexels"
            else _normalize_pixabay_candidate(raw, media_type)
        )

        if not normalized["url"]:
            continue

        local_path = _cache_path(provider, media_type, query, normalized["candidate_id"])
        cached = os.path.exists(local_path)

        if not cached:
            try:
                logger.info(
                    "Downloading %s candidate %s for scene=%s -> %s",
                    media_type,
                    normalized["candidate_id"],
                    scene_id,
                    local_path,
                )
                _download_file(normalized["url"], local_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to download candidate %s for scene=%s: %s",
                    normalized["candidate_id"],
                    scene_id,
                    exc,
                )
                continue

        normalized["local_path"] = local_path
        normalized["cached"] = cached
        candidates.append(normalized)

    return {"scene_id": scene_id, "provider": provider, "candidates": candidates}


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download candidate media for every scene in a media plan.

    Args:
        input_json: Must contain "run_id", "topic", and a non-empty
            "media_plan" list. May contain "candidates_per_scene".

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "media_plan"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        media_plan = input_json["media_plan"]
        candidates_per_scene = int(
            input_json.get("candidates_per_scene", DEFAULT_CANDIDATES_PER_SCENE)
        )

        if not isinstance(media_plan, list) or not media_plan:
            raise ContractError("media_plan must be a non-empty list")

        downloads: List[Dict[str, Any]] = []
        for scene_plan in media_plan:
            keywords = list(scene_plan.get("search_keywords", [])) + list(
                scene_plan.get("alternative_keywords", [])
            )
            keywords = keywords or [topic]

            # "_forced_provider" is set per-scene by the media engine's
            # Fallback/re-search loop (core.pipeline_controller) on retry
            # attempts, so each attempt tries a *specific* provider instead
            # of always restarting from MEDIA_PROVIDER_PRIORITY[0]. Absent
            # on a normal (first-attempt) call, so default behavior is
            # unchanged.
            forced_provider = scene_plan.get("_forced_provider")
            providers = [forced_provider] if forced_provider else None

            result = _download_scene_media(
                scene_id=scene_plan["scene_id"],
                media_type=scene_plan.get("media_type", "image"),
                keywords=keywords,
                candidates_per_scene=candidates_per_scene,
                negative_keywords=scene_plan.get("negative_keywords", []),
                providers=providers,
            )
            downloads.append(result)

        total_candidates = sum(len(d["candidates"]) for d in downloads)
        logger.info(
            "Media downloaded for run_id=%s topic='%s' -> %d scenes, %d total candidates",
            run_id,
            topic,
            len(downloads),
            total_candidates,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "downloads": downloads,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Media Downloader contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Media Downloader failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
