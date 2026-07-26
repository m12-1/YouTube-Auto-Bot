"""
modules/ai_media_verification/ai_media_verification.py

For every scene, scores each quality-filtered candidate against its
narration sentence using a vision-capable AI model, and returns the
best match, its score/reason, and the rejected candidates.

The vision model call goes through `shared.gemini_client`, which is
intentionally abstract (`generate_vision_score`) so a different model
provider can be swapped in later without touching this module's
contract. If no API key is configured or the call fails for a given
candidate, a deterministic keyword-overlap heuristic is used instead.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "storyboard": [ {"scene_id": str, "narration": str, "keywords": [str,...]}, ... ],
        "filtered": [
            {
                "scene_id": str,
                "accepted_candidates": [ {"candidate_id": str, "url": str, ...}, ... ],
                "rejected_candidates": [...]
            },
            ...
        ]
    }

input_json (optional key, takes priority over "filtered" when present):
    {
        "ranked": [
            {
                "scene_id": str,
                "ranked_candidates": [ {"candidate_id": str, "url": str, "rank_score": float, ...}, ... ]
            },
            ...
        ]
    }

    "ranked" is media_ranker's output (Stage 5 -- Semantic Ranking): each
    scene's candidates already sorted by final_rank_score and capped to
    MEDIA_MAX_VERIFIED_CANDIDATES, so Gemini Vision only ever scores the
    top slice instead of every quality-filtered candidate. "filtered" is
    kept as a fallback so callers that skip ranking (and existing tests)
    still work unchanged.

output_json:
    {
        "status": "success" | "error",
        "module": "ai_media_verification",
        "data": {
            "run_id": str,
            "topic": str,
            "verifications": [
                {
                    "scene_id": str,
                    "best_media": {...candidate...} | null,
                    "score": float,
                    "reason": str,
                    "rejected_candidates": [ {"candidate_id": str, "score": float, "reason": str}, ... ],
                    "source": "ai" | "heuristic_fallback"
                },
                ...
            ]
        },
        "error": str | null
    }
"""

from __future__ import annotations

import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from config import pipeline_config, settings
from shared.gemini_client import (
    GeminiUnavailableError,
    generate_vision_verification,
    get_gemini_api_keys,
)
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "ai_media_verification"


def _forbidden_match(candidate: Dict[str, Any], forbidden_objects: List[str]) -> Optional[str]:
    """
    Cheap, free pre-filter run BEFORE any Gemini call: if the
    candidate's own provider metadata (tags/alt text, carried through
    as "source_text" by media_downloader) mentions a forbidden object,
    reject it outright without spending AI verification budget on it.

    Args:
        candidate: A media candidate dict.
        forbidden_objects: Scene Analyzer's forbidden object/setting list.

    Returns:
        The forbidden term matched, or None if the candidate is clean.
    """
    if not forbidden_objects:
        return None
    haystack = f"{candidate.get('source_text', '')} {candidate.get('url', '')}".lower()
    for forbidden in forbidden_objects:
        needle = forbidden.strip().lower()
        if needle and needle in haystack:
            return needle
    return None


def _keyword_overlap_score(
    narration: str, candidate: Dict[str, Any], forbidden_objects: List[str]
) -> Tuple[float, str]:
    """
    Deterministic fallback scorer (used only if Gemini is unavailable):
    how many narration words appear in the candidate's own search
    keywords / URL slug, penalized if a forbidden object is mentioned.

    Args:
        narration: The scene's narration sentence.
        candidate: A media candidate dict.
        forbidden_objects: Scene Analyzer's forbidden object/setting list.

    Returns:
        A tuple of (score between 0.0 and 1.0, human-readable reason).
    """
    forbidden_hit = _forbidden_match(candidate, forbidden_objects)
    if forbidden_hit:
        return 0.0, f"Heuristic fallback: matched forbidden term '{forbidden_hit}'."

    narration_words = set(re.findall(r"[a-zA-Z']+", narration.lower()))
    candidate_text = (
        f"{candidate.get('url', '')} {candidate.get('candidate_id', '')} "
        f"{candidate.get('source_text', '')}"
    ).lower()
    candidate_words = set(re.findall(r"[a-zA-Z']+", candidate_text))

    overlap = narration_words & candidate_words
    score = min(1.0, 0.4 + 0.15 * len(overlap))
    reason = (
        f"Heuristic keyword overlap: {len(overlap)} shared term(s)."
        if overlap
        else "Heuristic fallback: no strong keyword overlap, default baseline score."
    )
    return round(score, 3), reason


def _rotate_keys(api_keys: List[str], track: int) -> List[str]:
    """
    Rotate the ordered key list so a given worker "track" starts from a
    different key than the others, instead of every concurrent scene
    racing for the same first key. This is a pure re-ordering of the
    SAME keys/models already configured -- no key is added, removed, or
    swapped, and the full fallback chain is still tried if the rotated
    starting key is unavailable/rate-limited.

    Args:
        api_keys: The full ordered list of configured Gemini keys.
        track: Worker track index (0, 1, 2, ...), typically
            `scene_index % AI_VERIFICATION_MAX_WORKERS`.

    Returns:
        The same keys, rotated so index `track % len(api_keys)` comes first.
    """
    if not api_keys or track <= 0:
        return api_keys
    offset = track % len(api_keys)
    return api_keys[offset:] + api_keys[:offset]


def _extract_local_frame_bytes(local_path: str) -> Optional[Tuple[bytes, str]]:
    """
    Grab a real JPEG frame straight from the media file that's ALREADY
    been downloaded to disk by `media_downloader`, instead of trusting a
    remote "thumbnail_url".

    This exists because remote thumbnail URLs are unreliable in
    practice -- e.g. Pixabay's video API doesn't return a direct
    thumbnail field at all, so a URL has to be *guessed* from
    "picture_id" using an external CDN convention that can 404, redirect,
    or simply not correspond to that candidate. When that guessed URL
    fails, the old code silently fell back to the raw candidate "url" --
    which for a VIDEO candidate is the .mp4 file itself, something
    Gemini's inline_data image parts can never decode. The net effect
    (visible in production logs) was ai_media_verification falling back
    to the much weaker keyword-overlap heuristic for entire batches of
    otherwise-good candidates, which is exactly what let a visually
    wrong clip (e.g. a moon shot for an unrelated scene) get selected.

    Pulling a frame locally sidesteps all of that: the file is already
    on disk, so there's no network round-trip to fail, and it always
    reflects the ACTUAL candidate rather than a best-effort guess.

    Returns:
        (jpeg_bytes, "image/jpeg") on success, or None if the file is
        missing/unreadable/not a supported type -- callers should fall
        back to the remote thumbnail_url path in that case.
    """
    if not local_path or not os.path.isfile(local_path):
        return None

    is_video = local_path.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi"))

    try:
        if is_video:
            import cv2  # type: ignore[import-untyped]

            capture = cv2.VideoCapture(local_path)
            try:
                frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                # Sample from the middle of the clip rather than frame 0,
                # which is sometimes a black/fade-in frame.
                target_frame = frame_count // 2 if frame_count > 1 else 0
                capture.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                success, frame = capture.read()
                if not success or frame is None:
                    return None
                ok, encoded = cv2.imencode(".jpg", frame)
                if not ok:
                    return None
                return bytes(encoded.tobytes()), "image/jpeg"
            finally:
                capture.release()
        else:
            from PIL import Image
            import io

            with Image.open(local_path) as img:
                buffer = io.BytesIO()
                img.convert("RGB").save(buffer, format="JPEG")
                return buffer.getvalue(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001 - any failure just means "no local frame"
        logger.debug("Local frame extraction failed for '%s': %s", local_path, exc)
        return None


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _score_candidate_with_ai(
    narration: str,
    candidate: Dict[str, Any],
    required_objects: List[str],
    environment: str,
    forbidden_objects: List[str],
    topic: str,
    scientific_domain: str,
    previous_scene: str,
    next_scene: str,
    required_visual_style: str = "",
    forbidden_styles: Optional[List[str]] = None,
    key_track: int = 0,
) -> Dict[str, Any]:
    """
    Score a single candidate using multi-signal Gemini vision scoring:
    semantic fit, scientific-domain lock, visual continuity with the
    previous/next scene, visual-style consistency, cinematic quality,
    and a generic-stock-footage check — not just one generic relevance
    question.

    Raises:
        GeminiUnavailableError: If no key is configured or the call fails.
    """
    # Every configured Gemini key, in priority order. shared.gemini_client
    # now does the rotation itself INSIDE one call: it tries every model
    # in the fallback chain on the current key first, and only moves on
    # to the next key once that key's whole model chain is exhausted
    # (e.g. rate-limited) -- so we just hand it the full ordered list
    # instead of looping over keys here ourselves.
    api_keys = get_gemini_api_keys(
        settings.GEMINI_KEY_IMAGE,
        settings.GEMINI_KEY_ADVANCED,
        settings.GEMINI_KEY_FILTER,
        settings.GEMINI_KEY_FILTER_2,
        settings.GEMINI_KEY_LIGHT,
    )
    if not api_keys:
        raise GeminiUnavailableError("No Gemini API key configured for ai_media_verification.")

    api_keys = _rotate_keys(api_keys, key_track)

    # THE FIX: prefer "thumbnail_url" (always a real JPEG/PNG image, set by
    # media_downloader for both video and image candidates) over "url",
    # which for video candidates is the .mp4 file itself. Gemini's
    # inline_data image parts cannot decode a video container, so passing
    # "url" straight through for videos meant the vision call either had
    # nothing valid to look at, or (before this fix) never actually looked
    # at anything at all. Falls back to "url" only for older cached
    # candidate shapes that predate the thumbnail_url field.
    # THE FIX: prefer a frame pulled directly from the ALREADY-DOWNLOADED
    # local file over any remote "thumbnail_url" guess. Local extraction
    # can't 404/redirect/mismatch the way a derived CDN URL can, and the
    # file is already on disk by this point in the pipeline anyway.
    local_frame = _extract_local_frame_bytes(candidate.get("local_path", ""))

    image_url = candidate.get("thumbnail_url") or candidate.get("url", "")
    if not image_url and local_frame is None:
        raise GeminiUnavailableError("Candidate has no image/thumbnail URL to verify.")

    return generate_vision_verification(
        narration_sentence=narration,
        image_url=image_url,
        api_key=api_keys,
        required_objects=required_objects,
        environment=environment,
        forbidden_objects=forbidden_objects,
        topic=topic,
        scientific_domain=scientific_domain,
        previous_scene=previous_scene,
        next_scene=next_scene,
        required_visual_style=required_visual_style,
        forbidden_styles=forbidden_styles,
        prefetched_image=local_frame,
    )


def _score_candidate(
    narration: str,
    candidate: Dict[str, Any],
    required_objects: List[str],
    environment: str,
    forbidden_objects: List[str],
    topic: str,
    scientific_domain: str,
    previous_scene: str,
    next_scene: str,
    required_visual_style: str = "",
    forbidden_styles: Optional[List[str]] = None,
    key_track: int = 0,
) -> Tuple[Dict[str, Any], str]:
    """
    Score a candidate: reject instantly (no AI call) if a forbidden
    object is already evident from provider metadata; otherwise prefer
    the multi-signal AI verifier, falling back to the keyword-overlap
    heuristic on any AI failure.

    Returns:
        A tuple of (score_result dict with "score"/"reason", source string).
    """
    forbidden_hit = _forbidden_match(candidate, forbidden_objects)
    if forbidden_hit:
        return (
            {"score": 0.0, "reason": f"Rejected pre-Gemini: matched forbidden term '{forbidden_hit}'."},
            "forbidden_prefilter",
        )

    try:
        result = _score_candidate_with_ai(
            narration,
            candidate,
            required_objects,
            environment,
            forbidden_objects,
            topic,
            scientific_domain,
            previous_scene,
            next_scene,
            required_visual_style,
            forbidden_styles,
            key_track,
        )
        return result, "ai"
    except GeminiUnavailableError as exc:
        logger.warning(
            "ai_media_verification falling back to heuristic for candidate=%s: %s",
            candidate.get("candidate_id"),
            exc,
        )
        score, reason = _keyword_overlap_score(narration, candidate, forbidden_objects)
        return {"score": score, "reason": reason}, "heuristic_fallback"


def _verify_scene(
    scene_id: str,
    narration: str,
    accepted_candidates: List[Dict[str, Any]],
    required_objects: List[str],
    environment: str,
    forbidden_objects: List[str],
    topic: str,
    scientific_domain: str,
    previous_scene: str,
    next_scene: str,
    required_visual_style: str = "",
    forbidden_styles: Optional[List[str]] = None,
    key_track: int = 0,
) -> Dict[str, Any]:
    """
    Score all accepted candidates for one scene and pick the best one
    that clears the minimum relevance threshold (Fallback stage): a
    candidate that doesn't reach `MEDIA_MIN_VERIFICATION_SCORE` is
    treated the same as having no usable media for this scene, rather
    than being used just to fill the gap.

    Args:
        scene_id: The storyboard scene identifier.
        narration: The scene's narration sentence.
        accepted_candidates: Quality-filtered candidates for this scene.
        required_objects: Scene Analyzer's expected objects/subjects.
        environment: Scene Analyzer's expected environment/setting.
        forbidden_objects: Scene Analyzer's forbidden objects/settings.
        topic: The video's overall topic.
        scientific_domain: The video's Global Topic Understanding domain.
        previous_scene: Previous scene's narration (Visual Consistency Engine).
        next_scene: Next scene's narration (Visual Consistency Engine).
        required_visual_style: This scene's locked canonical visual style.
        forbidden_styles: Style names that must NOT appear for this video.
        key_track: Worker track index used to rotate which Gemini key this
            scene's calls start from, so concurrent scenes spread across
            keys instead of racing for the same one (see `_rotate_keys`).

    Returns:
        A verification result dict for this scene.
    """
    if not accepted_candidates:
        return {
            "scene_id": scene_id,
            "best_media": None,
            "score": 0.0,
            "reason": "No quality-approved candidates available for this scene.",
            "rejected_candidates": [],
            "source": "n/a",
        }

    scored: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    for candidate in accepted_candidates:
        score_result, source = _score_candidate(
            narration,
            candidate,
            required_objects,
            environment,
            forbidden_objects,
            topic,
            scientific_domain,
            previous_scene,
            next_scene,
            required_visual_style,
            forbidden_styles,
            key_track,
        )
        scored.append((candidate, score_result, source))

    scored.sort(key=lambda item: item[1].get("score", 0.0), reverse=True)
    best_candidate, best_score_result, best_source = scored[0]
    best_score = best_score_result.get("score", 0.0)

    rejected = [
        {
            "candidate_id": candidate.get("candidate_id"),
            "score": score_result.get("score", 0.0),
            "reason": score_result.get("reason", ""),
        }
        for candidate, score_result, _ in scored[1:]
    ]

    threshold = (
        pipeline_config.MEDIA_MIN_VERIFICATION_SCORE_HEURISTIC
        if best_source == "heuristic_fallback"
        else pipeline_config.MEDIA_MIN_VERIFICATION_SCORE_TOPIC_FALLBACK
        if best_candidate.get("is_topic_fallback")
        else pipeline_config.MEDIA_MIN_VERIFICATION_SCORE
    )
    if best_score < threshold:
        # Fallback stage: nothing cleared the bar, so this scene gets no
        # media rather than settling for a weak match. video_composer /
        # video_renderer already handle a scene with no verified media by
        # skipping it instead of failing the whole render.
        rejected.insert(
            0,
            {
                "candidate_id": best_candidate.get("candidate_id"),
                "score": best_score,
                "reason": best_score_result.get("reason", ""),
            },
        )
        return {
            "scene_id": scene_id,
            "best_media": None,
            "score": best_score,
            "reason": (
                f"No candidate reached the minimum relevance score "
                f"({best_score} < {threshold}). Best attempt: "
                f"{best_score_result.get('reason', '')}"
            ),
            "rejected_candidates": rejected,
            "source": best_source,
        }

    return {
        "scene_id": scene_id,
        "best_media": best_candidate,
        "score": best_score,
        "reason": best_score_result.get("reason", ""),
        "rejected_candidates": rejected,
        "source": best_source,
    }


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verify quality-filtered media candidates against each scene's
    narration using an (abstracted) AI vision model.

    Args:
        input_json: Must contain "run_id", "topic", "storyboard", and
            "filtered" (see `media_quality_filter` output shape).

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "storyboard", "filtered"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        storyboard = input_json["storyboard"]
        filtered = input_json["filtered"]

        if not isinstance(storyboard, list) or not isinstance(filtered, list):
            raise ContractError("storyboard and filtered must both be lists")

        topic_context = input_json.get("topic_context", {}) or {}
        scientific_domain = topic_context.get("scientific_domain", "")
        forbidden_styles = topic_context.get("forbidden_styles") or []

        ranked = input_json.get("ranked")
        if isinstance(ranked, list) and ranked:
            # Semantic Ranking already ran: use its top-slice, pre-sorted
            # candidates instead of the full quality-filtered list so
            # Gemini Vision only ever scores the best candidates per scene.
            filtered_by_scene = {
                entry["scene_id"]: {
                    "scene_id": entry["scene_id"],
                    "accepted_candidates": entry.get("ranked_candidates", []),
                }
                for entry in ranked
            }
        else:
            filtered_by_scene = {entry["scene_id"]: entry for entry in filtered}

        # Scenes are independent of each other (each only looks at its own
        # candidates plus the neighboring scenes' narration text, which is
        # read-only), so they're verified concurrently instead of one at a
        # time. This is the main reason a run could take 20-30+ minutes:
        # every candidate of every scene was a separate, sequential Gemini
        # call. `AI_VERIFICATION_MAX_WORKERS` (default 2) controls how many
        # scenes run at once; each worker's calls start from a different
        # Gemini key via `key_track` (see `_rotate_keys`) so two scenes
        # verifying at the same time don't both race for the same key
        # first. No model, key, or scoring logic changes -- only the
        # scheduling of the exact same per-scene work.
        max_workers = max(1, pipeline_config.AI_VERIFICATION_MAX_WORKERS)
        verifications: List[Optional[Dict[str, Any]]] = [None] * len(storyboard)

        def _verify_at(index: int) -> Dict[str, Any]:
            scene = storyboard[index]
            scene_id = scene["scene_id"]
            scene_filtered = filtered_by_scene.get(scene_id, {})
            accepted_candidates = scene_filtered.get("accepted_candidates", [])
            previous_scene = storyboard[index - 1].get("narration", "") if index > 0 else ""
            next_scene = storyboard[index + 1].get("narration", "") if index + 1 < len(storyboard) else ""
            return _verify_scene(
                scene_id,
                scene.get("narration", ""),
                accepted_candidates,
                scene.get("objects", []),
                scene.get("environment", ""),
                scene.get("forbidden", []),
                topic,
                scientific_domain,
                previous_scene,
                next_scene,
                scene.get("visual_style", "") or topic_context.get("visual_style_locked", ""),
                forbidden_styles,
                index % max_workers,
            )

        if max_workers == 1 or len(storyboard) <= 1:
            for index in range(len(storyboard)):
                verifications[index] = _verify_at(index)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(_verify_at, index): index for index in range(len(storyboard))
                }
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    verifications[index] = future.result()

        scenes_without_media = sum(1 for v in verifications if v["best_media"] is None)
        logger.info(
            "AI media verification complete for run_id=%s topic='%s' "
            "-> %d scenes verified, %d without usable media",
            run_id,
            topic,
            len(verifications),
            scenes_without_media,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "verifications": verifications,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("AI Media Verification contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("AI Media Verification failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
