"""
tests/test_part2_modules.py

Smoke tests for the Part 2 content generation engine:
script_generator -> script_reviewer -> seo_generator ->
storyboard_generator -> media_planner -> media_downloader ->
media_quality_filter -> ai_media_verification -> voice_generator ->
subtitle_generator -> video_composer -> video_renderer ->
quality_inspector.

These tests exercise each module's public `run(input_json)` contract
in isolation (chaining real outputs between modules) so they also
double as an integration check that JSON keys line up end-to-end.
None of these tests require live API keys — every AI/network-backed
module has a deterministic fallback path exercised here.
"""

from __future__ import annotations

from modules.ai_media_verification.ai_media_verification import run as run_ai_media_verification
from modules.media_downloader.media_downloader import run as run_media_downloader
from modules.media_planner.media_planner import run as run_media_planner
from modules.media_quality_filter.media_quality_filter import run as run_media_quality_filter
from modules.quality_inspector.quality_inspector import run as run_quality_inspector
from modules.script_generator.script_generator import run as run_script_generator
from modules.script_reviewer.script_reviewer import run as run_script_reviewer
from modules.seo_generator.seo_generator import run as run_seo_generator
from modules.storyboard_generator.storyboard_generator import run as run_storyboard_generator
from modules.subtitle_generator.subtitle_generator import run as run_subtitle_generator
from modules.video_composer.video_composer import run as run_video_composer
from modules.video_renderer.video_renderer import run as run_video_renderer
from modules.voice_generator.voice_generator import run as run_voice_generator

_RUN_ID = "test-run-part2"
_TOPIC = "How batteries degrade over time"
_VERIFIED_FACTS = [
    {"fact_id": "f1", "text": "Lithium-ion batteries lose capacity through repeated charge cycles.", "confidence": 0.9, "verified": True},
    {"fact_id": "f2", "text": "Heat accelerates battery degradation significantly.", "confidence": 0.85, "verified": True},
]


def _generate_script() -> dict:
    result = run_script_generator(
        {"run_id": _RUN_ID, "topic": _TOPIC, "verified_facts": _VERIFIED_FACTS, "category": "technology"}
    )
    assert result["status"] == "success"
    return result["data"]["script"]


def test_script_generator_produces_scene_breakdown() -> None:
    script = _generate_script()
    assert script["scene_breakdown"]
    assert script["narration"]


def test_script_reviewer_improves_script() -> None:
    script = _generate_script()
    result = run_script_reviewer({"run_id": _RUN_ID, "topic": _TOPIC, "script": script})
    assert result["status"] == "success"
    assert "quality_score" in result["data"]
    assert result["data"]["script"]["narration"]


def test_seo_generator_returns_required_fields() -> None:
    script = _generate_script()
    result = run_seo_generator({"run_id": _RUN_ID, "topic": _TOPIC, "script": script})
    assert result["status"] == "success"
    seo = result["data"]["seo"]
    for key in ("title", "description", "tags", "hashtags", "ctr_prediction", "seo_score"):
        assert key in seo


def test_storyboard_generator_builds_timed_scenes() -> None:
    script = _generate_script()
    result = run_storyboard_generator({"run_id": _RUN_ID, "topic": _TOPIC, "script": script})
    assert result["status"] == "success"
    storyboard = result["data"]["storyboard"]
    assert len(storyboard) == len(script["scene_breakdown"])
    assert storyboard[0]["start_time"] == 0.0


def test_storyboard_generator_rejects_empty_scene_breakdown() -> None:
    result = run_storyboard_generator(
        {"run_id": _RUN_ID, "topic": _TOPIC, "script": {"narration": "x", "scene_breakdown": []}}
    )
    assert result["status"] == "error"


def _build_storyboard() -> list:
    script = _generate_script()
    result = run_storyboard_generator({"run_id": _RUN_ID, "topic": _TOPIC, "script": script})
    return result["data"]["storyboard"]


def test_media_planner_produces_one_entry_per_scene() -> None:
    storyboard = _build_storyboard()
    result = run_media_planner({"run_id": _RUN_ID, "topic": _TOPIC, "storyboard": storyboard})
    assert result["status"] == "success"
    assert len(result["data"]["media_plan"]) == len(storyboard)


def test_media_downloader_returns_one_entry_per_scene_even_without_keys() -> None:
    storyboard = _build_storyboard()
    plan_result = run_media_planner({"run_id": _RUN_ID, "topic": _TOPIC, "storyboard": storyboard})
    media_plan = plan_result["data"]["media_plan"]

    result = run_media_downloader({"run_id": _RUN_ID, "topic": _TOPIC, "media_plan": media_plan})
    assert result["status"] == "success"
    assert len(result["data"]["downloads"]) == len(media_plan)


def test_media_quality_filter_handles_empty_candidates() -> None:
    downloads = [{"scene_id": "scene-1", "provider": "unavailable", "candidates": []}]
    result = run_media_quality_filter({"run_id": _RUN_ID, "topic": _TOPIC, "downloads": downloads})
    assert result["status"] == "success"
    assert result["data"]["filtered"][0]["accepted_candidates"] == []


def test_ai_media_verification_reports_missing_media_gracefully() -> None:
    storyboard = _build_storyboard()
    filtered = [
        {"scene_id": scene["scene_id"], "accepted_candidates": [], "rejected_candidates": []}
        for scene in storyboard
    ]
    result = run_ai_media_verification(
        {"run_id": _RUN_ID, "topic": _TOPIC, "storyboard": storyboard, "filtered": filtered}
    )
    assert result["status"] == "success"
    assert all(v["best_media"] is None for v in result["data"]["verifications"])


def test_voice_generator_falls_back_to_simulated_timings() -> None:
    script = _generate_script()
    result = run_voice_generator({"run_id": _RUN_ID, "topic": _TOPIC, "script": script})
    assert result["status"] == "success"
    assert result["data"]["word_timings"]
    assert result["data"]["source"] in {"edge_tts", "simulated_fallback"}


def test_subtitle_generator_chunks_words() -> None:
    script = _generate_script()
    voice_result = run_voice_generator({"run_id": _RUN_ID, "topic": _TOPIC, "script": script})
    word_timings = voice_result["data"]["word_timings"]

    result = run_subtitle_generator({"run_id": _RUN_ID, "topic": _TOPIC, "word_timings": word_timings})
    assert result["status"] == "success"
    assert result["data"]["subtitle_timeline"]


def test_video_composer_flags_missing_media() -> None:
    storyboard = _build_storyboard()
    verifications = [
        {"scene_id": scene["scene_id"], "best_media": None, "score": 0.0, "reason": "n/a"}
        for scene in storyboard
    ]
    result = run_video_composer(
        {
            "run_id": _RUN_ID,
            "topic": _TOPIC,
            "storyboard": storyboard,
            "verifications": verifications,
            "audio_path": "/tmp/does-not-exist.mp3",
            "subtitle_timeline": [{"line_id": 1, "text": "hi", "start": 0.0, "end": 1.0, "words": []}],
        }
    )
    assert result["status"] == "success"
    assert result["data"]["render_plan"]["missing_media_scene_ids"]


def test_video_renderer_falls_back_to_manifest_when_media_missing(tmp_path, monkeypatch) -> None:
    from config import pipeline_config

    monkeypatch.setattr(pipeline_config, "VIDEO_OUTPUT_DIR", str(tmp_path))

    render_plan = {
        "resolution": {"width": 1080, "height": 1920},
        "fps": 30,
        "audio_track": "/tmp/does-not-exist.mp3",
        "total_duration_seconds": 5.0,
        "tracks": {"video": [{"scene_id": "s1", "start": 0.0, "end": 5.0, "media_path": None}], "subtitles": []},
        "missing_media_scene_ids": ["s1"],
    }
    seo = {"title": "t", "description": "d", "tags": [], "hashtags": [], "ctr_prediction": 5.0, "seo_score": 0.5}

    result = run_video_renderer({"run_id": "test-run-renderer", "topic": _TOPIC, "render_plan": render_plan, "seo": seo})
    assert result["status"] == "success"
    assert result["data"]["rendered"] is False
    assert result["data"]["source"] == "manifest_only"


def test_quality_inspector_fails_when_media_missing() -> None:
    render_plan = {
        "resolution": {"width": 1080, "height": 1920},
        "fps": 30,
        "total_duration_seconds": 5.0,
        "tracks": {"video": [{"scene_id": "s1", "start": 0.0, "end": 5.0}], "subtitles": [{"start": 0.0, "end": 5.0}]},
        "missing_media_scene_ids": ["s1"],
    }
    result = run_quality_inspector(
        {"run_id": _RUN_ID, "topic": _TOPIC, "render_plan": render_plan, "rendered": False}
    )
    assert result["status"] == "success"
    assert result["data"]["verdict"] == "FAIL"
    assert result["data"]["checks"]["no_missing_scenes"] is False
    assert result["data"]["checks"]["no_rendering_failures"] is False


def test_quality_inspector_passes_when_everything_checks_out() -> None:
    render_plan = {
        "resolution": {"width": 1080, "height": 1920},
        "fps": 30,
        "total_duration_seconds": 5.0,
        "tracks": {
            "video": [{"scene_id": "s1", "start": 0.0, "end": 5.0}],
            "subtitles": [{"start": 0.0, "end": 5.0}],
        },
        "missing_media_scene_ids": [],
    }
    result = run_quality_inspector(
        {"run_id": _RUN_ID, "topic": _TOPIC, "render_plan": render_plan, "rendered": True}
    )
    assert result["status"] == "success"
    assert result["data"]["verdict"] == "PASS"
