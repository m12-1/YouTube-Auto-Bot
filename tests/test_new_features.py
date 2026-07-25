"""
tests/test_new_features.py

Tests for the new features added to the video pipeline:
- Fallback media path borrowing
- Visual memory
- Quality Inspector two-tier logic
"""

from unittest import mock

from config import pipeline_config
from modules.video_renderer.video_renderer import (
    _fill_missing_media_from_neighbors,
    VisualMemory,
)
from modules.quality_inspector.quality_inspector import _check_no_missing_scenes

def test_visual_memory():
    vm = VisualMemory()
    vm.register("path1.mp4", style="realistic", motion_type="pan")
    assert vm.is_duplicate("path1.mp4") is True
    assert vm.is_duplicate("path2.mp4") is False
    
    assert vm.check_style_consistency("realistic") is True
    assert vm.check_style_consistency("cartoon") is False

def test_fill_missing_uses_fallbacks_first():
    video_track = [
        {"scene_id": "1", "media_path": None, "fallback_media_paths": ["fallback1.mp4", "fallback2.mp4"]},
        {"scene_id": "2", "media_path": "scene2.mp4", "fallback_media_paths": []}
    ]
    
    with mock.patch("os.path.isfile", return_value=True):
        filled = _fill_missing_media_from_neighbors(video_track)
        
        assert filled[0]["media_path"] == "fallback1.mp4"
        assert "_media_borrowed" not in filled[0]
        
def test_fill_missing_borrows_if_no_fallback():
    video_track = [
        {"scene_id": "1", "media_path": None, "fallback_media_paths": []},
        {"scene_id": "2", "media_path": "scene2.mp4", "fallback_media_paths": []}
    ]
    
    with mock.patch("os.path.isfile", return_value=True):
        filled = _fill_missing_media_from_neighbors(video_track)
        
        assert filled[0]["media_path"] == "scene2.mp4"
        assert filled[0].get("_media_borrowed") is True

def test_quality_inspector_two_tier_verdict():
    # Test PASS (0 missing)
    render_plan_pass = {
        "tracks": {"video": [{"scene_id": "1"}, {"scene_id": "2"}]},
        "missing_media_scene_ids": []
    }
    ok, reasons, stats = _check_no_missing_scenes(render_plan_pass)
    assert ok is True
    assert stats["missing_scene_count"] == 0
    
    # Test FAIL (> 5% missing, in this case 50%)
    render_plan_fail = {
        "tracks": {"video": [{"scene_id": "1"}, {"scene_id": "2"}]},
        "missing_media_scene_ids": ["1"]
    }
    ok, reasons, stats = _check_no_missing_scenes(render_plan_fail)
    assert ok is False
    assert stats["missing_scene_count"] == 1
    assert stats["missing_ratio"] == 0.5
