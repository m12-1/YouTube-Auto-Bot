"""
core/pipeline_controller.py

Orchestrates the pipeline by calling each module in sequence and
passing JSON between them. The controller never imports another
module's internals directly for business logic — it only invokes
each module's public `run(input_json)` function and forwards JSON.

Currently wired stages (Part 1 + Part 2 + Part 3):
    scheduler -> topic_memory -> topic_selector -> competitor_analyzer ->
    fact_collector -> fact_verifier -> script_generator ->
    script_reviewer -> seo_generator -> storyboard_generator ->
    media_planner -> media_downloader -> media_quality_filter ->
    ai_media_verification -> voice_generator -> subtitle_generator ->
    video_composer -> video_renderer -> quality_inspector -> publisher ->
    analytics_collector -> performance_analyzer -> learning_engine ->
    knowledge_base -> monthly_strategy

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
            "stages": {
                ...
            },
            "failed_stage": str | null
        },
        "error": str | null
    }
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from modules.ai_media_verification.ai_media_verification import run as run_ai_media_verification
from modules.analytics_collector.analytics_collector import run as run_analytics_collector
from modules.competitor_analyzer.competitor_analyzer import run as run_competitor_analyzer
from modules.fact_collector.fact_collector import run as run_fact_collector
from modules.fact_verifier.fact_verifier import run as run_fact_verifier
from modules.knowledge_base.knowledge_base import run as run_knowledge_base
from modules.learning_engine.learning_engine import run as run_learning_engine
from modules.media_downloader.media_downloader import run as run_media_downloader
from modules.media_planner.media_planner import run as run_media_planner
from modules.media_quality_filter.media_quality_filter import run as run_media_quality_filter
from modules.monthly_strategy.monthly_strategy import run as run_monthly_strategy
from modules.performance_analyzer.performance_analyzer import run as run_performance_analyzer
from modules.publisher.publisher import run as run_publisher
from modules.quality_inspector.quality_inspector import run as run_quality_inspector
from modules.scheduler.scheduler import run as run_scheduler
from modules.script_generator.script_generator import run as run_script_generator
from modules.script_reviewer.script_reviewer import run as run_script_reviewer
from modules.seo_generator.seo_generator import run as run_seo_generator
from modules.storyboard_generator.storyboard_generator import run as run_storyboard_generator
from modules.subtitle_generator.subtitle_generator import run as run_subtitle_generator
from modules.topic_memory.topic_memory import run as run_topic_memory
from modules.topic_selector.topic_selector import run as run_topic_selector
from modules.video_composer.video_composer import run as run_video_composer
from modules.video_renderer.video_renderer import run as run_video_renderer
from modules.voice_generator.voice_generator import run as run_voice_generator
from shared.json_contract import build_response
from shared.logger import get_logger

logger = get_logger(__name__)

MODULE_NAME = "pipeline_controller"

# Ordered list of (stage_name, stage_function) pairs. Each stage
# function must implement the run(input_json) -> output_json contract.
_STAGE_SEQUENCE: List[tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
    ("scheduler", run_scheduler),
    ("topic_memory", run_topic_memory),
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
    ("publisher", run_publisher),
    ("analytics_collector", run_analytics_collector),
    ("performance_analyzer", run_performance_analyzer),
    ("learning_engine", run_learning_engine),
    ("knowledge_base", run_knowledge_base),
    ("monthly_strategy", run_monthly_strategy),
]


def _merge_stage_output(
    accumulated_input: Dict[str, Any], stage_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge a stage's output data into the accumulated input for the
    next stage, without mutating the original dict.

    Args:
        accumulated_input: JSON payload accumulated so far.
        stage_data: The `data` field returned by the last stage.

    Returns:
        A new dict combining both, with `stage_data` taking precedence
        on key conflicts (stages produce the freshest values).
    """
    merged = dict(accumulated_input)
    merged.update(stage_data)
    return merged


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute the full pipeline sequence, stage by stage.

    Args:
        input_json: Optional dict of initial parameters forwarded to
            the Scheduler and subsequent stages.

    Returns:
        A standardized JSON response envelope containing every
        stage's output under `data.stages`, plus the name of the
        first failed stage (if any) under `data.failed_stage`.
    """
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
