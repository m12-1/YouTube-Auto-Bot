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
from typing import Any, Dict, List, Optional, Tuple

from config import pipeline_config, settings
from shared.gemini_client import (
    GeminiUnavailableError,
    generate_vision_verification,
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
    # Try every configured Gemini key in order (not just one): if a key's
    # whole model chain is exhausted (e.g. rate-limited), move on to the
    # next *key* before giving up and falling back to the heuristic.
    api_keys = [
        key
        for key in (
            settings.GEMINI_KEY_IMAGE,
            settings.GEMINI_KEY_ADVANCED,
            settings.GEMINI_KEY_FILTER,
            settings.GEMINI_KEY_FILTER_2,
            settings.GEMINI_KEY_LIGHT,
        )
        if key
    ]
    if not api_keys:
        raise GeminiUnavailableError("No Gemini API key configured for ai_media_verification.")

    url = candidate.get("url", "")
    if not url:
        raise GeminiUnavailableError("Candidate has no URL to verify.")

    last_error: Optional[Exception] = None
    for api_key in api_keys:
        try:
            return generate_vision_verification(
                narration_sentence=narration,
                image_url=url,
                api_key=api_key,
                required_objects=required_objects,
                environment=environment,
                forbidden_objects=forbidden_objects,
                topic=topic,
                scientific_domain=scientific_domain,
                previous_scene=previous_scene,
                next_scene=next_scene,
                required_visual_style=required_visual_style,
                forbidden_styles=forbidden_styles,
            )
        except GeminiUnavailableError as exc:
            last_error = exc
            logger.warning(
                "Gemini key ending '...%s' exhausted for ai_media_verification, "
                "trying next configured key: %s",
                api_key[-4:] if len(api_key) >= 4 else "****",
                exc,
            )
            continue

    raise GeminiUnavailableError(
        f"All configured Gemini keys exhausted for ai_media_verification: {last_error}"
    ) from last_error


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

        verifications: List[Dict[str, Any]] = []
        for index, scene in enumerate(storyboard):
            scene_id = scene["scene_id"]
            scene_filtered = filtered_by_scene.get(scene_id, {})
            accepted_candidates = scene_filtered.get("accepted_candidates", [])
            previous_scene = storyboard[index - 1].get("narration", "") if index > 0 else ""
            next_scene = storyboard[index + 1].get("narration", "") if index + 1 < len(storyboard) else ""
            verifications.append(
                _verify_scene(
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
                )
            )

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
