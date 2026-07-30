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

        # ثلاثة ملفات منفصلة لذاكرة التحقق البصري (بدل خلط كل شيء في
        # run_state.json)، حسب المبدأ: العقد (المتطلبات) ثابت بعد اعتماد
        # السكربت، الذاكرة البصرية تتطور بعد كل مشهد مقبول، وسجل المحاولات
        # يحفظ كل مرشح (مقبول أو مرفوض) لغرض التدقيق. هذا يمنع الخلط بين
        # "المطلوب" و"الموجود" عند إرسال السياق لاحقًا إلى Gemini.
        self.contracts_path = os.path.join(run_dir, "scene_contracts.json")
        self.memory_path = os.path.join(run_dir, "accepted_visual_memory.json")
        self.attempts_path = os.path.join(run_dir, "verification_attempts.json")
        self.contracts: dict = {}
        self.memory: dict = {}
        self.attempts: list = []
        if os.path.exists(self.contracts_path):
            with open(self.contracts_path, "r", encoding="utf-8") as f:
                self.contracts = json.load(f)
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                self.memory = json.load(f)
        if os.path.exists(self.attempts_path):
            with open(self.attempts_path, "r", encoding="utf-8") as f:
                self.attempts = json.load(f)
        self.data = {
            "used_keywords": {},    # scene_id -> [keywords تم تجربتها]
            "scenes": {},           # scene_id -> {clip_path, start, end, score}
            "used_source_keys": [], # ["source:id", ...] فيديوهات مصدر استُخدمت فعلاً في مشهد مقبول
            "narration": None,
            "video_title": None,
            "youtube_keywords": [],
            # scene_id -> ملخص بصري JSON مختصر (subject/action/setting/entities)
            # لآخر مرشح مقبول في ذلك المشهد. يُستخدم لمقارنة المشهد التالي
            # بصريًا مع المشهد السابق (اتساق عبر المشاهد)، وليس فقط مطابقة
            # كل مشهد لنصه بمعزل عن بقية الفيديو.
            "scene_visual_summaries": {},
            # قائمة scene_id بترتيب ظهورها في السكربت، تُملأ مرة واحدة من
            # main.py بعد بناء الخطة، لتمكين معرفة "المشهد السابق" لأي مشهد.
            "scene_order": [],
        }
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data.update(loaded)
            # توافقية مع ملفات run_state.json قديمة أُنشئت قبل إضافة هذا الحقل
            # (تشغيلات سابقة على نسخة الكود قبل إصلاح تكرار المشهد).
            self.data.setdefault("used_source_keys", [])
            # توافقية مع ملفات run_state.json قديمة أُنشئت قبل إضافة الاتساق
            # البصري عبر المشاهد (cross-scene consistency).
            self.data.setdefault("scene_visual_summaries", {})
            self.data.setdefault("scene_order", [])

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

    def set_scene_order(self, scene_ids: list[str]):
        """تُستدعى مرة واحدة بعد بناء خطة المشاهد (main.py) لتسجيل ترتيبها،
        فيُستخدم لاحقًا لمعرفة \"المشهد السابق\" لأي مشهد عبر get_previous_scene_visual_summary."""
        with self._lock:
            self.data["scene_order"] = list(scene_ids)
            self.save()

    def set_scene_visual_summary(self, scene_id: str, summary: dict | None):
        """يخزّن الملخص البصري (subject/action/setting/entities) للمرشح الذي
        تم قبوله فعليًا لهذا المشهد، ليُستخدم في مقارنة المشهد التالي معه."""
        with self._lock:
            if summary:
                self.data["scene_visual_summaries"][scene_id] = summary
                self.save()

    def get_previous_scene_visual_summary(self, scene_id: str) -> dict | None:
        """يعيد الملخص البصري للمشهد السابق مباشرة (حسب scene_order) لهذا
        المشهد، أو None إن كان هذا أول مشهد أو لم يُسجَّل ترتيب/ملخص بعد."""
        with self._lock:
            order = self.data.get("scene_order", [])
            if scene_id not in order:
                return None
            idx = order.index(scene_id)
            if idx == 0:
                return None
            prev_id = order[idx - 1]
            return self.data.get("scene_visual_summaries", {}).get(prev_id)

    def set_scene_result(self, scene_id: str, clip_path: str, start: float, end: float, score: float,
                          needs_manual_review: bool = False, requires_attribution: bool = False,
                          attribution_text: str = None, media_type: str = "video",
                          images: list[str] | None = None):
        with self._lock:
            self.data["scenes"][scene_id] = {
                "clip_path": clip_path, "start": start, "end": end, "score": score,
                "needs_manual_review": needs_manual_review,
                "requires_attribution": requires_attribution,
                "attribution_text": attribution_text,
                # media_type: "video" (افتراضي، السلوك القديم كما هو) أو
                # "image_sequence" (خطة الصور البديلة الصارمة - انظر
                # ai_media_verification._try_image_fallback). images تُملأ
                # فقط في حالة image_sequence.
                "media_type": media_type,
                "images": images or [],
            }
            self.save()

    # --- scene_contracts.json: ثابت بعد اعتماد السكربت --------------------
    def set_scene_contract(self, scene_id: str, contract: dict):
        """يسجّل عقد المشهد (النص/الكيان المطلوب فعليًا actor_ref/الكلمات
        المفتاحية) مرة واحدة فقط بعد اعتماد السكربت. لا يُستبدل إن كان مسجّلًا
        فعلًا، لأن هذا الملف يجب أن يبقى ثابتًا طوال بقية التشغيل ولا يُعدَّل
        بناءً على أي تحقق بصري لاحق (المتطلبات لا تُخفَّف تلقائيًا)."""
        with self._lock:
            if scene_id in self.contracts:
                return
            self.contracts[scene_id] = contract
            with open(self.contracts_path, "w", encoding="utf-8") as f:
                json.dump(self.contracts, f, ensure_ascii=False, indent=2)

    def get_scene_contract(self, scene_id: str) -> dict | None:
        with self._lock:
            return self.contracts.get(scene_id)

    # --- accepted_visual_memory.json: يتطور بعد قبول كل مشهد --------------
    def set_scene_visual_memory(self, scene_id: str, entry: dict | None):
        """يخزّن ذاكرة المشهد المقبول (الملخص البصري + هوية الكيان المكتشفة
        فعليًا) لتُستخدم كمرجع اتساق عند فحص المشهد التالي، منفصلة عن عقد
        المتطلبات الأصلي."""
        if not entry:
            return
        with self._lock:
            self.memory[scene_id] = entry
            with open(self.memory_path, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
            # نُبقي أيضًا على الحقل القديم في run_state.json (scene_visual_summaries)
            # للتوافقية مع أي كود آخر (مثل تقرير main.py النهائي) يقرأ منه مباشرة.
            self.data.setdefault("scene_visual_summaries", {})[scene_id] = entry.get("visual_summary")
            self.save()

    def get_previous_scene_visual_memory(self, scene_id: str) -> dict | None:
        """يعيد ذاكرة المشهد السابق مباشرة (نص + هوية + ملخص بصري) من
        accepted_visual_memory.json، أو None إن لم توجد."""
        with self._lock:
            order = self.data.get("scene_order", [])
            if scene_id not in order:
                return None
            idx = order.index(scene_id)
            if idx == 0:
                return None
            prev_id = order[idx - 1]
            return self.memory.get(prev_id)

    # --- verification_attempts.json: كل مرشح قُيِّم، مقبولًا كان أو مرفوضًا -
    def log_verification_attempt(self, scene_id: str, attempt: dict):
        with self._lock:
            record = {"scene_id": scene_id, **attempt}
            self.attempts.append(record)
            with open(self.attempts_path, "w", encoding="utf-8") as f:
                json.dump(self.attempts, f, ensure_ascii=False, indent=2)
