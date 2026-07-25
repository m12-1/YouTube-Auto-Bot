"""
modules/media_ranker/media_ranker.py

Media Ranking (Stage 5 -- Semantic Ranking).

Scores every quality-filtered candidate independently against the
scene's requirements and combines those independent scores into a
single `final_rank_score`, so candidates reach `ai_media_verification`
already ordered by actual relevance rather than by whichever order
Pexels/Pixabay happened to return them in.

This module is deliberately generic: every signal it computes is
derived from data already produced upstream for THIS scene (Scene
Analyzer's objects/environment/forbidden list, Media Planner's
keywords, Global Topic Understanding's scientific_domain/visual_theme,
provider metadata on the candidate itself). Nothing here is specific
to any topic, subject, or domain -- the same scoring code runs whether
the scene is about a black hole, a war, a hormone, or a painting.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "filtered": [
            {
                "scene_id": str,
                "accepted_candidates": [ {...candidate...}, ... ],
                "rejected_candidates": [...]
            },
            ...
        ]
    }

input_json (optional keys, all improve ranking quality when present):
    {
        "media_plan": [
            {
                "scene_id": str, "primary_keywords": [str,...],
                "secondary_keywords": [str,...], "required_objects": [str,...],
                "forbidden_objects": [str,...], "environment": str,
                "camera_movement": str, "media_type": "video"|"image",
                "duration_seconds": float
            },
            ...
        ],
        "storyboard": [
            {"scene_id": str, "narration": str, "visual_description": str}, ...
        ],
        "topic_context": {"scientific_domain": str, "visual_theme": str}
    }

output_json:
    {
        "status": "success" | "error",
        "module": "media_ranker",
        "data": {
            "run_id": str,
            "topic": str,
            "ranked": [
                {
                    "scene_id": str,
                    "ranked_candidates": [
                        {...candidate..., "rank_score": float, "rank_breakdown": {...}},
                        ...
                    ],
                    "low_rank_candidates": [
                        {"candidate_id": str, "rank_score": float, "rank_breakdown": {...}}, ...
                    ]
                },
                ...
            ]
        },
        "error": str | null
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger

logger = get_logger(__name__)

MODULE_NAME = "media_ranker"

_WORD_RE = re.compile(r"[a-zA-Z']+")

# Generic English function words -- not topic-specific vocabulary. Without
# filtering these, any reference text built from full sentences (narration,
# visual_description) is dominated by words like "the"/"you"/"it" that can
# never meaningfully appear in a stock provider's tag list, which silently
# drags every overlap-based score toward zero regardless of subject.
_STOPWORDS = frozenset(
    """
    a an the this that these those is are was were be been being to of in on
    at for with without by from as it its it's you your yours we our ours
    they their theirs he she his her him them i me my mine and or but if
    then than so not no yes do does did done doing have has had having will
    would shall should can could may might must just very really quite here
    there what which who whom whose why how when where all any both each
    few more most other some such only own same too also into onto out up
    down over under again further once about above below between through
    """.split()
)


def _tokenize(text: str) -> set:
    """Lowercase word-tokenize free text for overlap-based scoring, dropping
    generic stopwords so scores reflect meaningful vocabulary overlap."""
    if not text:
        return set()
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _candidate_text(candidate: Dict[str, Any]) -> str:
    """Whatever free text is available about a candidate (tags/alt/url)."""
    return f"{candidate.get('source_text', '')} {candidate.get('url', '')}".lower()


def _overlap_score(reference_words: set, candidate_text: str) -> float:
    """
    Generic word-overlap relevance score in [0, 1] using the Dice
    coefficient (2 * shared / (|reference| + |candidate|)) rather than
    plain recall. Recall alone (matched / len(reference)) collapses
    toward zero whenever the reference set is large relative to a
    candidate's short tag list -- e.g. a reference built from full
    narration sentences vs. a candidate with a dozen one-word tags --
    even when every one of the candidate's tags is a genuine hit. Dice
    balances both set sizes so a strong, on-topic tag list still scores
    well regardless of how verbose the reference text was.

    Returns the configured neutral baseline when there is nothing to
    compare against (empty reference), so an absent signal never
    silently drops a candidate's score to zero.
    """
    if not reference_words:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE
    candidate_words = _tokenize(candidate_text)
    if not candidate_words:
        return 0.0
    matched = len(reference_words & candidate_words)
    if matched == 0:
        return 0.0
    dice = (2 * matched) / (len(reference_words) + len(candidate_words))
    return round(min(1.0, dice), 4)


def _list_match_score(terms: List[str], candidate_text: str) -> float:
    """Fraction of a list of phrases (objects, actions, ...) found in candidate text."""
    cleaned = [t.strip().lower() for t in terms if t and t.strip()]
    if not cleaned:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE
    hits = sum(1 for term in cleaned if term in candidate_text)
    return round(min(1.0, hits / len(cleaned)), 4)


def _forbidden_gate(forbidden_objects: List[str], candidate_text: str) -> Tuple[float, List[str]]:
    """
    Hard multiplicative gate: any forbidden object present in the
    candidate's own text collapses its final score toward zero instead
    of merely lowering one component. Mirrors the same forbidden-list
    concept Scene Analyzer / media_downloader / ai_media_verification
    already apply, so a candidate can't slip through ranking just
    because its forbidden-object score was outweighed by other signals.

    Returns:
        (gate_multiplier in {0.0, 1.0}, list of matched forbidden terms)
    """
    matches = []
    for forbidden in forbidden_objects:
        needle = forbidden.strip().lower()
        if needle and needle in candidate_text:
            matches.append(needle)
    return (0.0 if matches else 1.0), matches


def _generic_stock_penalty(candidate_text: str) -> float:
    """
    Score in [0, 1] where 1.0 means the candidate does NOT look like
    generic, subject-less stock filler (per configurable term list),
    and lower values mean more generic-stock indicator terms matched.
    """
    terms = [t.strip().lower() for t in pipeline_config.MEDIA_GENERIC_STOCK_TERMS if t.strip()]
    if not terms:
        return 1.0
    hits = sum(1 for term in terms if term in candidate_text)
    if hits == 0:
        return 1.0
    return round(max(0.0, 1.0 - (hits / len(terms))), 4)


def _style_match_score(candidate_text: str, locked_style: str) -> float:
    """
    Soft score in [0, 1] for whether a candidate's own tags/URL text
    read as the video's locked visual style (Real Footage/CGI/3D Render/
    2D Illustration/Vector/Cartoon). 1.0 if a locked-style term is
    present, the configured neutral baseline if the candidate's text
    doesn't clearly signal any style at all (most stock tags don't spell
    out "real footage" explicitly), 0.0 if it clearly signals a
    DIFFERENT style. This is a soft preference only -- the hard block on
    mismatched styles already happened earlier via forbidden/negative
    keywords (see storyboard_generator's style lock), so this never lets
    a wrong-style candidate back in on its own.
    """
    if not locked_style:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE

    own_terms = [t.lower() for t in pipeline_config.MEDIA_STYLE_TERMS.get(locked_style, [])]
    if any(term in candidate_text for term in own_terms):
        return 1.0

    for category, terms in pipeline_config.MEDIA_STYLE_TERMS.items():
        if category == locked_style:
            continue
        if any(term.lower() in candidate_text for term in terms):
            return 0.0

    return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE


def _cinematic_quality_score(candidate_text: str) -> float:
    """
    Soft score in [0, 1]: fraction of configured "looks professional"
    indicator terms found in the candidate's own tags/URL text. This is
    a crude text-only proxy (weighted low on purpose) -- the real
    cinematic-quality judgment happens in Gemini Vision's own
    "cinematic_quality" check downstream, which actually looks at the
    media.
    """
    terms = [t.strip().lower() for t in pipeline_config.MEDIA_CINEMATIC_TERMS if t.strip()]
    if not terms:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE
    hits = sum(1 for term in terms if term in candidate_text)
    if hits == 0:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE
    return round(min(1.0, hits / max(2, len(terms) // 3)), 4)


def _resolution_score(candidate: Dict[str, Any]) -> float:
    """
    Score in [0, 1]: 0 at the configured minimum resolution, rising to
    1.0 as actual resolution reaches (or exceeds) 2x the minimum on
    both dimensions. Purely relative to configured thresholds -- no
    fixed pixel numbers baked into this module.
    """
    width = candidate.get("width", 0) or 0
    height = candidate.get("height", 0) or 0
    min_w = max(1, pipeline_config.MEDIA_MIN_WIDTH)
    min_h = max(1, pipeline_config.MEDIA_MIN_HEIGHT)
    if width <= 0 or height <= 0:
        return 0.0
    width_ratio = min(1.0, max(0.0, (width - min_w) / min_w)) if width >= min_w else 0.0
    height_ratio = min(1.0, max(0.0, (height - min_h) / min_h)) if height >= min_h else 0.0
    if width < min_w or height < min_h:
        return 0.0
    return round(min(1.0, (width_ratio + height_ratio) / 2), 4)


def _orientation_score(candidate: Dict[str, Any]) -> float:
    """1.0 if the candidate matches the required orientation, else 0.0."""
    width = candidate.get("width", 0) or 0
    height = candidate.get("height", 0) or 0
    if not width or not height:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE
    is_portrait = height >= width
    required = pipeline_config.MEDIA_REQUIRED_ORIENTATION
    if required == "portrait":
        return 1.0 if is_portrait else 0.0
    if required == "landscape":
        return 1.0 if not is_portrait else 0.0
    return 1.0


def _duration_score(candidate: Dict[str, Any], target_duration: float) -> float:
    """
    Score in [0, 1] for how close a video candidate's own duration is
    to the scene's target duration. Images (no duration_seconds) and
    scenes without a target both fall back to the neutral baseline,
    since duration fit doesn't apply to them.
    """
    duration = candidate.get("duration_seconds")
    if duration is None or not target_duration or target_duration <= 0:
        return pipeline_config.MEDIA_RANK_NEUTRAL_BASELINE
    diff_ratio = abs(duration - target_duration) / target_duration
    return round(max(0.0, 1.0 - min(1.0, diff_ratio)), 4)


def _score_candidate(
    candidate: Dict[str, Any],
    semantic_reference_words: set,
    domain_words: set,
    required_objects: List[str],
    required_actions: List[str],
    environment_words: set,
    camera_words: set,
    visual_theme_words: set,
    forbidden_objects: List[str],
    target_duration: float,
    locked_style: str = "",
) -> Tuple[float, Dict[str, float]]:
    """
    Compute every independent sub-score for one candidate and combine
    them into a single final_rank_score using configurable weights.

    Returns:
        (final_rank_score, breakdown dict of every sub-score)
    """
    text = _candidate_text(candidate)

    semantic_similarity = _overlap_score(semantic_reference_words, text)
    scientific_domain = _overlap_score(domain_words, text)
    required_objects_match = _list_match_score(required_objects, text)
    required_actions_match = _list_match_score(required_actions, text)
    environment_match = _overlap_score(environment_words, text)
    camera_style_match = _overlap_score(camera_words, text)
    visual_theme_match = _overlap_score(visual_theme_words, text)
    resolution = _resolution_score(candidate)
    orientation = _orientation_score(candidate)
    duration = _duration_score(candidate, target_duration)
    generic_stock = _generic_stock_penalty(text)
    style_match = _style_match_score(text, locked_style)
    cinematic_quality = _cinematic_quality_score(text)
    forbidden_gate, forbidden_hits = _forbidden_gate(forbidden_objects, text)

    weighted_components: List[Tuple[float, float]] = [
        (pipeline_config.MEDIA_RANK_WEIGHT_SEMANTIC_SIMILARITY, semantic_similarity),
        (pipeline_config.MEDIA_RANK_WEIGHT_SCIENTIFIC_DOMAIN, scientific_domain),
        (pipeline_config.MEDIA_RANK_WEIGHT_REQUIRED_OBJECTS, required_objects_match),
        (pipeline_config.MEDIA_RANK_WEIGHT_REQUIRED_ACTIONS, required_actions_match),
        (pipeline_config.MEDIA_RANK_WEIGHT_ENVIRONMENT, environment_match),
        (pipeline_config.MEDIA_RANK_WEIGHT_CAMERA_STYLE, camera_style_match),
        (pipeline_config.MEDIA_RANK_WEIGHT_VISUAL_THEME, visual_theme_match),
        (pipeline_config.MEDIA_RANK_WEIGHT_RESOLUTION, resolution),
        (pipeline_config.MEDIA_RANK_WEIGHT_ORIENTATION, orientation),
        (pipeline_config.MEDIA_RANK_WEIGHT_DURATION, duration),
        (pipeline_config.MEDIA_RANK_WEIGHT_GENERIC_STOCK_PENALTY, generic_stock),
        (pipeline_config.MEDIA_RANK_WEIGHT_STYLE_MATCH, style_match),
        (pipeline_config.MEDIA_RANK_WEIGHT_CINEMATIC_QUALITY, cinematic_quality),
    ]
    total_weight = sum(weight for weight, _ in weighted_components)
    base_score = (
        sum(weight * score for weight, score in weighted_components) / total_weight
        if total_weight > 0
        else 0.0
    )

    final_score = round(base_score * forbidden_gate, 4)

    breakdown = {
        "semantic_similarity_score": semantic_similarity,
        "scientific_domain_score": scientific_domain,
        "required_objects_match": required_objects_match,
        "required_actions_match": required_actions_match,
        "environment_match": environment_match,
        "camera_style_match": camera_style_match,
        "visual_theme_match": visual_theme_match,
        "resolution_score": resolution,
        "orientation_score": orientation,
        "duration_score": duration,
        "generic_stock_penalty": generic_stock,
        "style_match_score": style_match,
        "cinematic_quality_score": cinematic_quality,
        "forbidden_objects_penalty": forbidden_gate,
        "forbidden_matches": forbidden_hits,
    }
    return final_score, breakdown


def _rank_scene(
    scene_id: str,
    accepted_candidates: List[Dict[str, Any]],
    scene_plan: Dict[str, Any],
    scene_story: Dict[str, Any],
    topic: str,
    scientific_domain: str,
    visual_theme: str,
) -> Dict[str, Any]:
    """Rank every accepted candidate for one scene and split by threshold."""
    if not accepted_candidates:
        return {"scene_id": scene_id, "ranked_candidates": [], "low_rank_candidates": []}

    primary = scene_plan.get("primary_keywords") or scene_plan.get("search_keywords") or []
    secondary = scene_plan.get("secondary_keywords") or []
    required_objects = scene_plan.get("required_objects") or []
    forbidden_objects = scene_plan.get("forbidden_objects") or []
    environment = scene_plan.get("environment", "")
    camera_movement = scene_plan.get("camera_movement", "")
    target_duration = scene_plan.get("duration_seconds") or 0.0

    narration = scene_story.get("narration", "")
    visual_description = scene_story.get("visual_description", "")
    locked_style = scene_plan.get("visual_style") or scene_story.get("visual_style") or ""

    # "Required actions" has no dedicated field upstream -- the general,
    # topic-agnostic proxy is whatever descriptive words Scene Analyzer
    # wrote in visual_description that aren't already covered by the
    # object/environment/keyword fields (those cover *what*; the leftover
    # words in visual_description tend to describe *what's happening*).
    covered_words = _tokenize(" ".join(required_objects + primary + secondary)) | _tokenize(environment)
    action_words = [w for w in _tokenize(visual_description) if w not in covered_words and len(w) > 2]

    semantic_reference_words = _tokenize(" ".join(primary + secondary)) | _tokenize(topic)
    domain_words = _tokenize(scientific_domain)
    environment_words = _tokenize(environment)
    camera_words = _tokenize(camera_movement.replace("_", " "))
    visual_theme_words = _tokenize(visual_theme)

    scored: List[Tuple[Dict[str, Any], float, Dict[str, Any]]] = []
    for candidate in accepted_candidates:
        score, breakdown = _score_candidate(
            candidate,
            semantic_reference_words,
            domain_words,
            required_objects,
            action_words,
            environment_words,
            camera_words,
            visual_theme_words,
            forbidden_objects,
            target_duration,
            locked_style,
        )
        scored.append((candidate, score, breakdown))

    scored.sort(key=lambda item: item[1], reverse=True)

    ranked_candidates = []
    low_rank_candidates = []
    for candidate, score, breakdown in scored:
        enriched = dict(candidate)
        enriched["rank_score"] = score
        enriched["rank_breakdown"] = breakdown
        if score >= pipeline_config.MEDIA_MIN_RANK_SCORE:
            ranked_candidates.append(enriched)
        else:
            low_rank_candidates.append(enriched)

    # Gemini Vision must only ever see the top slice, never every
    # candidate that happened to clear the (lower) rank threshold.
    ranked_candidates = ranked_candidates[: max(0, pipeline_config.MEDIA_MAX_VERIFIED_CANDIDATES)]

    return {
        "scene_id": scene_id,
        "ranked_candidates": ranked_candidates,
        "low_rank_candidates": low_rank_candidates,
    }


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rank every quality-filtered candidate per scene by an independent,
    multi-signal `final_rank_score` and keep only the top slice for
    downstream AI verification.

    Args:
        input_json: Must contain "run_id", "topic", and a non-empty
            "filtered" list (see `media_quality_filter` output shape).
            "media_plan", "storyboard", and "topic_context" are optional
            but sharply improve ranking quality when supplied.

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "filtered"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        filtered = input_json["filtered"]

        if not isinstance(filtered, list) or not filtered:
            raise ContractError("filtered must be a non-empty list")

        media_plan_by_scene = {p["scene_id"]: p for p in (input_json.get("media_plan") or [])}
        storyboard_by_scene = {s["scene_id"]: s for s in (input_json.get("storyboard") or [])}
        topic_context = input_json.get("topic_context", {}) or {}
        scientific_domain = topic_context.get("scientific_domain", "")
        visual_theme = topic_context.get("visual_theme", "")

        ranked: List[Dict[str, Any]] = []
        total_ranked = 0
        total_low_rank = 0

        for scene_filtered in filtered:
            scene_id = scene_filtered["scene_id"]
            accepted = scene_filtered.get("accepted_candidates", [])
            scene_plan = media_plan_by_scene.get(scene_id, {})
            scene_story = storyboard_by_scene.get(scene_id, {})

            result = _rank_scene(
                scene_id,
                accepted,
                scene_plan,
                scene_story,
                topic,
                scientific_domain,
                visual_theme,
            )
            total_ranked += len(result["ranked_candidates"])
            total_low_rank += len(result["low_rank_candidates"])
            ranked.append(result)

        logger.info(
            "Media ranking complete for run_id=%s topic='%s' "
            "-> %d candidates ranked above threshold, %d below, across %d scenes",
            run_id,
            topic,
            total_ranked,
            total_low_rank,
            len(ranked),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "ranked": ranked,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Media Ranker contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Media Ranker failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
