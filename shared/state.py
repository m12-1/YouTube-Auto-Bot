"""
حالة التشغيل (run state) المشتركة بين المراحل: تتبع الكلمات المفتاحية المستخدمة
لكل مشهد، ومسارات المقاطع النهائية، لمنع إعادة استخدام نفس الكلمة مرتين.
"""

import json
import os


class RunState:
    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.path = os.path.join(run_dir, "run_state.json")
        self.data = {
            "used_keywords": {},   # scene_id -> [keywords تم تجربتها]
            "scenes": {},          # scene_id -> {clip_path, start, end, score}
            "narration": None,
            "video_title": None,
            "youtube_keywords": [],
        }
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def mark_keyword_used(self, scene_id: str, keyword: str):
        self.data["used_keywords"].setdefault(scene_id, [])
        if keyword not in self.data["used_keywords"][scene_id]:
            self.data["used_keywords"][scene_id].append(keyword)
        self.save()

    def keyword_already_tried(self, scene_id: str, keyword: str) -> bool:
        return keyword in self.data["used_keywords"].get(scene_id, [])

    def set_scene_result(self, scene_id: str, clip_path: str, start: float, end: float, score: float):
        self.data["scenes"][scene_id] = {
            "clip_path": clip_path, "start": start, "end": end, "score": score,
        }
        self.save()
