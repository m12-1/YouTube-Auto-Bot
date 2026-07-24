"""
tests/test_pipeline_controller.py

Basic smoke tests for the Part 1 pipeline:
scheduler -> topic_selector -> competitor_analyzer -> fact_collector
-> fact_verifier -> pipeline_controller.
"""

from __future__ import annotations

from core.pipeline_controller import run as run_pipeline
from modules.competitor_analyzer.competitor_analyzer import run as run_competitor_analyzer
from modules.fact_collector.fact_collector import run as run_fact_collector
from modules.fact_verifier.fact_verifier import run as run_fact_verifier
from modules.scheduler.scheduler import run as run_scheduler
from modules.topic_selector.topic_selector import run as run_topic_selector


def test_scheduler_returns_run_id() -> None:
    result = run_scheduler({})
    assert result["status"] == "success"
    assert result["data"]["run_id"]


def test_topic_selector_requires_run_id() -> None:
    result = run_topic_selector({})
    assert result["status"] == "error"


def test_topic_selector_honors_category_hint() -> None:
    result = run_topic_selector({"run_id": "test-run", "category_hint": "science"})
    assert result["status"] == "success"
    assert result["data"]["category"] == "science"


def test_competitor_analyzer_returns_valid_fallback_without_api_key() -> None:
    # No YOUTUBE_SEARCH_API_KEY is configured in this environment. The
    # module must degrade gracefully (empty competitor list) rather
    # than crash or fabricate fake data.
    result = run_competitor_analyzer({"run_id": "test-run", "topic": "Black holes"})
    assert result["status"] == "success"
    assert result["data"]["competitors"] == []
    assert result["data"]["saturation_level"] in {"low", "medium", "high"}


def test_fact_collector_respects_max_facts() -> None:
    result = run_fact_collector(
        {"run_id": "test-run", "topic": "Black holes", "max_facts": 2}
    )
    assert result["status"] == "success"
    assert len(result["data"]["raw_facts"]) == 2


def test_fact_verifier_filters_by_confidence() -> None:
    collected = run_fact_collector({"run_id": "test-run", "topic": "Black holes"})
    result = run_fact_verifier(
        {
            "run_id": "test-run",
            "topic": "Black holes",
            "raw_facts": collected["data"]["raw_facts"],
            "min_confidence": 0.0,
        }
    )
    assert result["status"] == "success"
    assert result["data"]["rejected_count"] == 0
    assert len(result["data"]["verified_facts"]) == len(collected["data"]["raw_facts"])


def test_full_pipeline_runs_successfully() -> None:
    result = run_pipeline({"category_hint": "technology", "max_facts": 3})
    assert result["status"] == "success"
    assert result["data"]["failed_stage"] is None
    assert set(result["data"]["stages"].keys()) == {
        "scheduler",
        "topic_selector",
        "competitor_analyzer",
        "fact_collector",
        "fact_verifier",
        "script_generator",
        "script_reviewer",
        "seo_generator",
        "storyboard_generator",
        "media_planner",
        "media_downloader",
        "media_quality_filter",
        "ai_media_verification",
        "voice_generator",
        "subtitle_generator",
        "video_composer",
        "video_renderer",
        "quality_inspector",
    }
    for stage_name, stage_result in result["data"]["stages"].items():
        assert stage_result["status"] == "success", f"stage '{stage_name}' failed: {stage_result.get('error')}"
