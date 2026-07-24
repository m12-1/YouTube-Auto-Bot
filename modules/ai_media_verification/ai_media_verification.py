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
from shared.gemini_client import GeminiUnavailableError, generate_vision_score, pick_api_key
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "ai_media_verification"


def _keyword_overlap_score(narration: str, candidate: Dict[str, Any]) -> Tuple[float, str]:
    """
    Deterministic fallback scorer: how many narration words appear in
    the candidate's own search keywords / URL slug.

    Args:
        narration: The scene's narration sentence.
        candidate: A media candidate dict.

    Returns:
        A tuple of (score between 0.0 and 1.0, human-readable reason).
    """
    narration_words = set(re.findall(r"[a-zA-Z']+", narration.lower()))
    candidate_text = f"{candidate.get('url', '')} {candidate.get('candidate_id', '')}".lower()
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
def _score_candidate_with_ai(narration: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """
    Score a single candidate against a narration sentence using the
    Gemini vision API.

    Args:
        narration: The scene's narration sentence.
        candidate: A media candidate dict (must include "url").

    Returns:
        A dict with "score" and "reason".

    Raises:
        GeminiUnavailableError: If no key is configured or the call fails.
    """
    api_key = pick_api_key(settings.GEMINI_KEY_IMAGE, settings.GEMINI_KEY_ADVANCED)
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for ai_media_verification.")

    url = candidate.get("url", "")
    if not url:
        raise GeminiUnavailableError("Candidate has no URL to verify.")

    return generate_vision_score(narration_sentence=narration, image_url=url, api_key=api_key)


def _score_candidate(narration: str, candidate: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """
    Score a candidate, preferring the AI vision model and falling back
    to the keyword-overlap heuristic on any failure.

    Args:
        narration: The scene's narration sentence.
        candidate: A media candidate dict.

    Returns:
        A tuple of (score_result dict with "score"/"reason", source string).
    """
    try:
        result = _score_candidate_with_ai(narration, candidate)
        return result, "ai"
    except GeminiUnavailableError as exc:
        logger.warning(
            "ai_media_verification falling back to heuristic for candidate=%s: %s",
            candidate.get("candidate_id"),
            exc,
        )
        score, reason = _keyword_overlap_score(narration, candidate)
        return {"score": score, "reason": reason}, "heuristic_fallback"


def _verify_scene(
    scene_id: str,
    narration: str,
    accepted_candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Score all accepted candidates for one scene and pick the best one.

    Args:
        scene_id: The storyboard scene identifier.
        narration: The scene's narration sentence.
        accepted_candidates: Quality-filtered candidates for this scene.

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
        score_result, source = _score_candidate(narration, candidate)
        scored.append((candidate, score_result, source))

    scored.sort(key=lambda item: item[1].get("score", 0.0), reverse=True)
    best_candidate, best_score_result, best_source = scored[0]

    rejected = [
        {
            "candidate_id": candidate.get("candidate_id"),
            "score": score_result.get("score", 0.0),
            "reason": score_result.get("reason", ""),
        }
        for candidate, score_result, _ in scored[1:]
    ]

    return {
        "scene_id": scene_id,
        "best_media": best_candidate,
        "score": best_score_result.get("score", 0.0),
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

        narration_by_scene = {scene["scene_id"]: scene.get("narration", "") for scene in storyboard}
        filtered_by_scene = {entry["scene_id"]: entry for entry in filtered}

        verifications: List[Dict[str, Any]] = []
        for scene_id, narration in narration_by_scene.items():
            scene_filtered = filtered_by_scene.get(scene_id, {})
            accepted_candidates = scene_filtered.get("accepted_candidates", [])
            verifications.append(_verify_scene(scene_id, narration, accepted_candidates))

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
