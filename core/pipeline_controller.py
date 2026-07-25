"""
core/pipeline_controller.py

Orchestrates the CURRENT PHASE pipeline: one topic in, one rendered
YouTube Short out. The controller never imports another module's
internals for business logic -- it only invokes each module's public
`run(input_json)` function and forwards JSON.

CURRENT PHASE stage sequence, and how it maps onto the conceptual
stages requested for this phase (some conceptual stages are folded
into a single existing module rather than split into new ones --
see the mapping notes below):

    scheduler             -> Scheduler
    topic_selector        -> Topic Selector
    competitor_analyzer   -> Competitor Analyzer
    fact_collector        -> Fact Collector
    fact_verifier         -> Fact Verifier   (its output IS the Knowledge Package)
    script_generator      -> Hook Generator + Narrative Planner + Script
                              Planner + Script Generator (hook/body/ending/
                              scene breakdown are produced together today)
    script_reviewer       -> Script Reviewer + Retention Optimizer (retention
                              is one of its existing review criteria)
    seo_generator         -> SEO Generator
    storyboard_generator  -> Scene Splitter + Scene Analyzer (splits narration
                              into timed scenes, extracts keywords/visual
                              intent per scene)
    media_planner         -> turns storyboard into concrete media requirements
    media_downloader      -> Media Search
    media_quality_filter  -> quality gate (resolution/orientation/duration/blur)
    media_ranker          -> Media Ranking (Semantic Ranking, Stage 5)
    ai_media_verification -> Media Relevance Checker

NOTE: media_downloader / media_quality_filter / media_ranker /
ai_media_verification are NOT run as a flat one-shot sequence like the
other stages. The controller wraps them in a Media Engine loop
(`_run_media_engine`) that implements the Stage 8 Fallback cycle:
search -> rank -> Gemini verify -> if a scene's best candidate doesn't
clear MEDIA_MIN_VERIFICATION_SCORE, regenerate its search queries and
retry, cycling through MEDIA_PROVIDER_PRIORITY, up to
MEDIA_MAX_SEARCH_ATTEMPTS retries per provider (all from
config.pipeline_config). Only scenes still unresolved carry over to the
next attempt; scenes that already found good media are locked in.
    voice_generator       -> Voice Generator
    subtitle_generator    -> Subtitle Generator + Subtitle Styler (word-level
                              highlight styling is produced with the timeline)
    video_composer        -> Media Timeline Builder + Visual Composer
    video_renderer        -> Video Renderer
    quality_inspector     -> Quality Inspector

NOTE: Some conceptual stage names (Knowledge Package, Hook Generator,
Narrative Planner, Script Planner, Retention Optimizer, Scene Analyzer,
Media Ranking, Media Relevance Checker, Media Timeline Builder,
Subtitle Styler) do not have a dedicated module yet -- they are
responsibilities already handled inside one of the modules above.
Splitting them into standalone modules would be an architecture
change, not a stabilization, so it was not done in this cleanup pass.

PHASE 2 (temporarily out of this pipeline, code preserved under
`future/modules/`, NOT deleted): topic_memory, analytics_collector,
performance_analyzer, learning_engine, knowledge_base, monthly_strategy,
publisher, plus the previously-orphaned ai_router, cache_manager,
cleanup_manager, config_manager, database, monitoring, prompt_manager,
report_generator.

Public contract:
    run(input_json) -> output_json

input_json (optional keys):
    {
        "run_id": str,
        "triggered_by": str,
        "category_hint": str,
        "max_facts": int,
        "min_confidence": float
    }

output_json:
    {
        "status": "success" | "error",
        "module": "pipeline_controller",
        "data": {
            "run_id": str,
            "stages": { ... },
            "failed_stage": str | null
        },
        "error": str | null
    }
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from modules.ai_media_verification.ai_media_verification import run as run_ai_media_verification
from modules.competitor_analyzer.competitor_analyzer import run as run_competitor_analyzer
from modules.fact_collector.fact_collector import run as run_fact_collector
from modules.fact_verifier.fact_verifier import run as run_fact_verifier
from config import pipeline_config
from modules.media_downloader.media_downloader import run as run_media_downloader
from modules.media_planner.media_planner import run as run_media_planner
from modules.media_quality_filter.media_quality_filter import run as run_media_quality_filter
from modules.media_ranker.media_ranker import run as run_media_ranker
from modules.quality_inspector.quality_inspector import run as run_quality_inspector
from modules.scheduler.scheduler import run as run_scheduler
from modules.script_generator.script_generator import run as run_script_generator
from modules.script_reviewer.script_reviewer import run as run_script_reviewer
from modules.seo_generator.seo_generator import run as run_seo_generator
from modules.storyboard_generator.storyboard_generator import run as run_storyboard_generator
from modules.subtitle_generator.subtitle_generator import run as run_subtitle_generator
from modules.topic_selector.topic_selector import run as run_topic_selector
from modules.video_composer.video_composer import run as run_video_composer
from modules.video_renderer.video_renderer import run as run_video_renderer
from modules.voice_generator.voice_generator import run as run_voice_generator
from shared.json_contract import build_response
from shared.logger import get_logger

# The publisher lives under future/modules because most Phase 2 modules
# (analytics, learning engine, etc.) aren't ready yet -- but publishing is
# the entire point of a "PUBLISH" run (main.py's --privacy flag, the
# YOUTUBE_OAUTH_* secrets wired through the CI workflow, the "mode=PUBLISH"
# log banner) and it already has a safe, complete implementation with a
# simulated fallback when credentials are missing. Leaving it unwired meant
# every run silently stopped at quality_inspector: the CLI would print
# "Pipeline finished successfully" and the video simply never reached
# YouTube, with nothing in the logs to explain why.
from future.modules.publisher.publisher import run as run_publisher

logger = get_logger(__name__)

MODULE_NAME = "pipeline_controller"

# Everything up to and including media_planner runs as a flat sequence.
# media_downloader / media_quality_filter / media_ranker /
# ai_media_verification are handled separately by `_run_media_engine`
# (see module docstring), then the flat sequence resumes with
# voice_generator.
_STAGE_SEQUENCE_PRE_MEDIA: List[tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
    ("scheduler", run_scheduler),
    ("topic_selector", run_topic_selector),
    ("competitor_analyzer", run_competitor_analyzer),
    ("fact_collector", run_fact_collector),
    ("fact_verifier", run_fact_verifier),
    ("script_generator", run_script_generator),
    ("script_reviewer", run_script_reviewer),
    ("seo_generator", run_seo_generator),
    ("storyboard_generator", run_storyboard_generator),
    ("media_planner", run_media_planner),
]

_STAGE_SEQUENCE_POST_MEDIA: List[tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
    ("voice_generator", run_voice_generator),
    ("subtitle_generator", run_subtitle_generator),
    ("video_composer", run_video_composer),
    ("video_renderer", run_video_renderer),
    ("quality_inspector", run_quality_inspector),
]


def _merge_stage_output(
    accumulated_input: Dict[str, Any], stage_data: Dict[str, Any]
) -> Dict[str, Any]:
    merged = dict(accumulated_input)
    merged.update(stage_data)
    return merged


def _run_stage_sequence(
    sequence: List[tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]],
    current_payload: Dict[str, Any],
    stage_outputs: Dict[str, Any],
) -> tuple[Dict[str, Any], str | None]:
    """
    Run a flat list of (stage_name, stage_fn) pairs in order, merging
    each stage's output into the payload for the next one. Stops at
    the first failure.

    Returns:
        (updated_payload, failed_stage_name_or_None)
    """
    for stage_name, stage_fn in sequence:
        logger.info("Pipeline running stage '%s'...", stage_name)

        try:
            stage_result = stage_fn(current_payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stage '%s' raised an unhandled exception.", stage_name)
            stage_outputs[stage_name] = build_response(
                module=stage_name, status="error", error=str(exc)
            )
            return current_payload, stage_name

        stage_outputs[stage_name] = stage_result

        if stage_result.get("status") != "success":
            logger.error(
                "Stage '%s' reported failure: %s",
                stage_name,
                stage_result.get("error"),
            )
            return current_payload, stage_name

        current_payload = _merge_stage_output(current_payload, stage_result.get("data", {}))

    return current_payload, None


def _rotate_scene_keywords(scene_plan: Dict[str, Any], rotation: int) -> Dict[str, Any]:
    """
    Regenerate a scene's search query for a retry attempt (Stage 8
    Fallback). Purely mechanical and topic-agnostic: it rotates which of
    the scene's already-extracted keyword pools (primary keywords /
    secondary keywords / media_planner's alternative keywords) leads the
    search, so a retry queries with a genuinely different combination
    instead of repeating the exact same failed query. No new keywords
    are invented and nothing here is specific to any subject.

    Args:
        scene_plan: One scene's media_plan entry.
        rotation: 0 on the first attempt against a given provider (pool
            order unchanged), 1/2/... on later cycles (pools rotated).

    Returns:
        A copy of scene_plan with "search_keywords" regenerated and
        "alternative_keywords" cleared (already folded into
        search_keywords, so media_downloader doesn't re-append them).
    """
    plan = dict(scene_plan)
    pools = [
        list(plan.get("primary_keywords") or []),
        list(plan.get("secondary_keywords") or []),
        list(plan.get("alternative_keywords") or []),
    ]
    if rotation:
        offset = rotation % len(pools)
        pools = pools[offset:] + pools[:offset]

    combined: List[str] = []
    for pool in pools:
        for keyword in pool:
            if keyword not in combined:
                combined.append(keyword)

    plan["search_keywords"] = combined or list(plan.get("search_keywords") or [])
    plan["alternative_keywords"] = []
    return plan


def _run_media_engine(
    payload: Dict[str, Any], stage_outputs: Dict[str, Any]
) -> tuple[Dict[str, Any], str | None]:
    """
    Media Engine: Search -> Rank -> Gemini Verify, with a real Stage 8
    Fallback cycle instead of leaving a scene without media after a
    single rejected attempt.

    For every unresolved scene: regenerate its search queries, retry
    against the current provider up to MEDIA_MAX_SEARCH_ATTEMPTS times,
    then move on to the next provider in MEDIA_PROVIDER_PRIORITY. A
    scene drops out of the loop as soon as it has a candidate that
    clears MEDIA_MIN_VERIFICATION_SCORE; only scenes still unresolved
    carry over to the next attempt. If every provider is exhausted, the
    scene is left without media (video_composer/video_renderer already
    handle that by skipping the scene rather than failing the render).

    Every tunable here (attempt count, provider list, candidate counts,
    thresholds) comes from config.pipeline_config -- nothing is
    hardcoded, and none of this logic references any specific topic,
    subject, or keyword.

    Returns:
        (updated_payload_with_verifications_merged_in, failed_stage_or_None)
    """
    media_plan = payload.get("media_plan") or []
    storyboard = payload.get("storyboard") or []
    if not media_plan:
        # No media planned at all -- let ai_media_verification's own
        # contract validation produce the proper error downstream.
        return payload, None

    providers = list(pipeline_config.MEDIA_PROVIDER_PRIORITY) or ["pexels", "pixabay"]
    attempts_per_provider = max(1, pipeline_config.MEDIA_MAX_SEARCH_ATTEMPTS)
    max_attempts = attempts_per_provider * len(providers)

    remaining_plan: Dict[str, Dict[str, Any]] = {p["scene_id"]: p for p in media_plan}
    resolved_verifications: Dict[str, Any] = {}
    last_verification_by_scene: Dict[str, Any] = {}

    attempt_index = 0
    while remaining_plan and attempt_index < max_attempts:
        provider_for_attempt = providers[attempt_index % len(providers)]
        rotation = attempt_index // len(providers)

        attempt_plan = []
        for scene_plan in remaining_plan.values():
            rotated = _rotate_scene_keywords(scene_plan, rotation)
            rotated["_forced_provider"] = provider_for_attempt
            attempt_plan.append(rotated)

        logger.info(
            "Media engine attempt %d/%d: provider='%s', %d scene(s) still unresolved.",
            attempt_index + 1,
            max_attempts,
            provider_for_attempt,
            len(attempt_plan),
        )

        download_input = dict(payload)
        download_input["media_plan"] = attempt_plan
        download_input["candidates_per_scene"] = pipeline_config.MEDIA_MAX_CANDIDATES
        download_result = run_media_downloader(download_input)
        stage_outputs["media_downloader"] = download_result
        if download_result.get("status") != "success":
            return payload, "media_downloader"

        filter_input = dict(payload)
        filter_input["downloads"] = download_result["data"]["downloads"]
        filter_result = run_media_quality_filter(filter_input)
        stage_outputs["media_quality_filter"] = filter_result
        if filter_result.get("status") != "success":
            return payload, "media_quality_filter"

        rank_input = dict(payload)
        rank_input["filtered"] = filter_result["data"]["filtered"]
        rank_input["media_plan"] = attempt_plan
        rank_result = run_media_ranker(rank_input)
        stage_outputs["media_ranker"] = rank_result
        if rank_result.get("status") != "success":
            return payload, "media_ranker"

        attempt_scene_ids = set(remaining_plan.keys())
        verify_input = dict(payload)
        verify_input["storyboard"] = [
            s for s in storyboard if s.get("scene_id") in attempt_scene_ids
        ]
        verify_input["ranked"] = rank_result["data"]["ranked"]
        verify_input["filtered"] = filter_result["data"]["filtered"]
        verify_result = run_ai_media_verification(verify_input)
        stage_outputs["ai_media_verification"] = verify_result
        if verify_result.get("status") != "success":
            return payload, "ai_media_verification"

        for verification in verify_result["data"]["verifications"]:
            scene_id = verification["scene_id"]
            last_verification_by_scene[scene_id] = verification
            if verification.get("best_media") is not None:
                resolved_verifications[scene_id] = verification
                remaining_plan.pop(scene_id, None)

        attempt_index += 1

    # Attempts exhausted: any scene still in remaining_plan keeps its
    # last (rejected) verification result rather than being dropped --
    # this is the "scene ends up with no usable media" outcome, not a
    # pipeline failure.
    for scene_id in remaining_plan:
        if scene_id in last_verification_by_scene:
            resolved_verifications[scene_id] = last_verification_by_scene[scene_id]

    ordered_verifications = [
        resolved_verifications[s["scene_id"]]
        for s in storyboard
        if s.get("scene_id") in resolved_verifications
    ]

    scenes_without_media = sum(1 for v in ordered_verifications if v.get("best_media") is None)
    logger.info(
        "Media engine finished after %d attempt(s) -> %d scene(s) resolved, "
        "%d without usable media.",
        attempt_index,
        len(ordered_verifications),
        scenes_without_media,
    )

    final_verification_stage = build_response(
        module="ai_media_verification",
        status="success",
        data={
            "run_id": payload.get("run_id"),
            "topic": payload.get("topic"),
            "verifications": ordered_verifications,
        },
    )
    stage_outputs["ai_media_verification"] = final_verification_stage

    updated_payload = _merge_stage_output(payload, final_verification_stage["data"])
    return updated_payload, None


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    input_json = input_json or {}
    stage_outputs: Dict[str, Any] = {}
    current_payload: Dict[str, Any] = dict(input_json)
    failed_stage: str | None = None

    current_payload, failed_stage = _run_stage_sequence(
        _STAGE_SEQUENCE_PRE_MEDIA, current_payload, stage_outputs
    )

    if failed_stage is None:
        current_payload, failed_stage = _run_media_engine(current_payload, stage_outputs)

    if failed_stage is None:
        # Defensive safety net: video_renderer requires "seo" (produced by
        # seo_generator, several stages earlier in _STAGE_SEQUENCE_PRE_MEDIA).
        # It should already be present in current_payload via the normal
        # stage-output merging, but re-assert it here from stage_outputs
        # directly so a missing/overwritten "seo" key can never surface as a
        # late, confusing "Missing required keys" failure at the very last
        # rendering stage instead of at seo_generator itself.
        if current_payload.get("seo") is None:
            seo_data = stage_outputs.get("seo_generator", {}).get("data", {}).get("seo")
            if seo_data is not None:
                current_payload["seo"] = seo_data

        current_payload, failed_stage = _run_stage_sequence(
            _STAGE_SEQUENCE_POST_MEDIA, current_payload, stage_outputs
        )

    # publisher is deliberately NOT in _STAGE_SEQUENCE: it must only ever
    # run after every prior stage succeeded AND the quality gate passed, and
    # it must be skippable for --dry-run without that counting as a pipeline
    # failure. Folding those two conditions into the generic stage loop
    # (which just checks status == "success") would either publish
    # FAIL-verdict videos or force a fake "error" for an intentional dry run.
    if failed_stage is None:
        quality_data = stage_outputs.get("quality_inspector", {}).get("data", {})
        verdict = quality_data.get("verdict")
        dry_run = bool(input_json.get("dry_run"))

        if dry_run:
            logger.info("Dry run requested -- skipping publish step.")
        elif verdict != "PASS":
            logger.warning(
                "Skipping publish step: quality_inspector verdict was '%s', not 'PASS'.",
                verdict,
            )
        else:
            logger.info("Pipeline running stage 'publisher'...")
            publisher_input = dict(current_payload)
            # publisher.py expects "video_output_path"; video_renderer's
            # envelope calls the same thing "final_video_path". Bridging the
            # field name here is just JSON plumbing between two existing
            # contracts, not business logic, so it stays in the controller.
            publisher_input["video_output_path"] = current_payload.get("final_video_path")

            try:
                publisher_result = run_publisher(publisher_input)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Stage 'publisher' raised an unhandled exception.")
                publisher_result = build_response(
                    module="publisher", status="error", error=str(exc)
                )

            stage_outputs["publisher"] = publisher_result

            if publisher_result.get("status") != "success":
                logger.error(
                    "Stage 'publisher' reported failure: %s",
                    publisher_result.get("error"),
                )
                failed_stage = "publisher"
            else:
                current_payload = _merge_stage_output(
                    current_payload, publisher_result.get("data", {})
                )
                logger.info(
                    "Published video_id=%s url=%s upload_status=%s",
                    publisher_result.get("data", {}).get("video_id"),
                    publisher_result.get("data", {}).get("video_url"),
                    publisher_result.get("data", {}).get("upload_status"),
                )

    overall_status = "error" if failed_stage else "success"
    error_message = (
        stage_outputs.get(failed_stage, {}).get("error") if failed_stage else None
    )

    data = {
        "run_id": current_payload.get("run_id"),
        "stages": stage_outputs,
        "failed_stage": failed_stage,
    }

    logger.info(
        "Pipeline finished with status='%s' failed_stage=%s",
        overall_status,
        failed_stage,
    )

    return build_response(
        module=MODULE_NAME, status=overall_status, data=data, error=error_message
    )
