"""
حالة التشغيل (run state) المشتركة بين المراحل: تتبع الكلمات المفتاحية المستخدمة
لكل مشهد، ومسارات المقاطع النهائية، لمنع إعادة استخدام نفس الكلمة مرتين، وتتبع
مصادر الوسائط (نفس الفيديو المصدر: source+id) التي استُخدمت فعلاً في مشهد مقبول
ضمن هذا الفيديو، لمنع اختيار نفس المصدر لأكثر من مشهد (إصلاح "تكرار المشهد").
"""

import json
import os
import threading


class RunState:
    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        # قفل بسيط يحمي كل قراءة/كتابة لبيانات الحالة والملف على القرص. لازم
        # الآن لأن main.py قد يعالج عدة مشاهد بالتوازي (كل مشهد في خيط)، وكلها
        # تشارك نفس كائن RunState (used_keywords/used_source_keys/scenes)، فبدون
        # قفل قد يحدث تعارض كتابة (race condition) يُفسد ملف run_state.json أو
        # يُفوّت تسجيل مصدر مستخدَم فعليًا. هذا لا يغيّر أي منطق قرار، فقط يمنع
        # تشابك الكتابة المتزامنة.
        self._lock = threading.RLock()
        os.makedirs(run_dir, exist_ok=True)
        self.path = os.path.join(run_dir, "run_state.json")
        self.data = {
            "used_keywords": {},    # scene_id -> [keywords تم تجربتها]
            "scenes": {},           # scene_id -> {clip_path, start, end, score}
            "used_source_keys": [], # ["source:id", ...] فيديوهات مصدر استُخدمت فعلاً في مشهد مقبول
            "narration": None,
            "video_title": None,
            "youtube_keywords": [],
        }
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data.update(loaded)
            # توافقية مع ملفات run_state.json قديمة أُنشئت قبل إضافة هذا الحقل
            # (تشغيلات سابقة على نسخة الكود قبل إصلاح تكرار المشهد).
            self.data.setdefault("used_source_keys", [])

    def save(self):
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def mark_keyword_used(self, scene_id: str, keyword: str):
        with self._lock:
            self.data["used_keywords"].setdefault(scene_id, [])
            if keyword not in self.data["used_keywords"][scene_id]:
                self.data["used_keywords"][scene_id].append(keyword)
            self.save()

    def keyword_already_tried(self, scene_id: str, keyword: str) -> bool:
        with self._lock:
            return keyword in self.data["used_keywords"].get(scene_id, [])

    def mark_source_used(self, source_key: str):
        """يسجّل أن هذا المصدر (source:id لفيديو معيّن) استُخدم فعلاً في مشهد
        مقبول ضمن الفيديو الحالي، بحيث تستبعده المشاهد التالية في نفس التشغيل
        من مرشحيها ولا يتكرر نفس المقطع في أكثر من مشهد."""
        if not source_key:
            return
        with self._lock:
            if source_key not in self.data["used_source_keys"]:
                self.data["used_source_keys"].append(source_key)
            self.save()

    def source_already_used(self, source_key: str) -> bool:
        with self._lock:
            return bool(source_key) and source_key in self.data["used_source_keys"]

    def set_scene_result(self, scene_id: str, clip_path: str, start: float, end: float, score: float,
                          needs_manual_review: bool = False, requires_attribution: bool = False,
                          attribution_text: str = None):
        with self._lock:
            self.data["scenes"][scene_id] = {
                "clip_path": clip_path, "start": start, "end": end, "score": score,
                "needs_manual_review": needs_manual_review,
                "requires_attribution": requires_attribution,
                "attribution_text": attribution_text,
            }
            self.save()
