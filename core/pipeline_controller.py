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
    media_quality_filter  -> Media Ranking
    ai_media_verification -> Media Relevance Checker
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
from modules.media_downloader.media_downloader import run as run_media_downloader
from modules.media_planner.media_planner import run as run_media_planner
from modules.media_quality_filter.media_quality_filter import run as run_media_quality_filter
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

_STAGE_SEQUENCE: List[tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
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
    ("media_downloader", run_media_downloader),
    ("media_quality_filter", run_media_quality_filter),
    ("ai_media_verification", run_ai_media_verification),
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


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    input_json = input_json or {}
    stage_outputs: Dict[str, Any] = {}
    current_payload: Dict[str, Any] = dict(input_json)
    failed_stage: str | None = None

    for stage_name, stage_fn in _STAGE_SEQUENCE:
        logger.info("Pipeline running stage '%s'...", stage_name)

        try:
            stage_result = stage_fn(current_payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stage '%s' raised an unhandled exception.", stage_name)
            stage_outputs[stage_name] = build_response(
                module=stage_name, status="error", error=str(exc)
            )
            failed_stage = stage_name
            break

        stage_outputs[stage_name] = stage_result

        if stage_result.get("status") != "success":
            logger.error(
                "Stage '%s' reported failure: %s",
                stage_name,
                stage_result.get("error"),
            )
            failed_stage = stage_name
            break

        current_payload = _merge_stage_output(
            current_payload, stage_result.get("data", {})
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
