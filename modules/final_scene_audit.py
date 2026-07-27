"""
المرحلة 7: مراجعة الجودة النهائية لكل مشهد على حدة (بعد تجميع الفيديو الكامل).
مفتاح مختلف: GEMINI_KEY_FILTER_2.

أي مشهد يحصل على تقييم < SCENE_AUDIT_ACCEPT_THRESHOLD:
  1. تُطلب كلمات مفتاحية بديلة لذلك المشهد فقط.
  2. إعادة تنفيذ المرحلة 5 (ai_media_verification) لهذا المشهد فقط.
  3. استبدال المقطع في الجدول الزمني وإعادة الرندر (video_composer.replace_scene_and_rerender).
  4. إعادة الفحص، حتى تنجح كل المشاهد أو حد أقصى MAX_SCENE_AUDIT_RETRIES.
"""

import logging

from shared.gemini_client import call_gemini_with_rotation, parse_json_response, upload_media_file
from config import SCENE_AUDIT_ACCEPT_THRESHOLD, MAX_SCENE_AUDIT_RETRIES

logger = logging.getLogger("modules.final_scene_audit")

STAGE = "final_scene_audit"

_AUDIT_PROMPT = """
هذا هو السكربت الكامل لفيديو قصير:
"{narration}"

المشاهد بالترتيب:
{scene_list}

شاهد الفيديو المرفق وقيّم كل مشهد على حدة من 10 من حيث مدى تطابقه مع النص المقابل له،
وجودة الانتقال، ومدى ملاءمته كفيديو "شورت" لليوتيوب.

أعد فقط كائن JSON:
{{
  "scenes": [
    {{"scene_id": "scene_1", "score": 0, "issue": "وصف مختصر للمشكلة إن وجدت أو فارغ"}}
  ]
}}
"""


def audit_video(final_video_path: str, narration: str, scenes: list[dict]) -> list[dict]:
    scene_list_str = "\n".join(f"- {s['id']}: {s['text']}" for s in scenes)
    prompt = _AUDIT_PROMPT.format(narration=narration, scene_list=scene_list_str)
    uploaded_video = upload_media_file(final_video_path)
    raw = call_gemini_with_rotation(STAGE, [prompt, uploaded_video], response_mime_type="application/json")
    return parse_json_response(raw).get("scenes", [])


def get_failed_scenes(audit_results: list[dict]) -> list[dict]:
    return [r for r in audit_results if r.get("score", 10) < SCENE_AUDIT_ACCEPT_THRESHOLD]


def request_alternative_keywords_for_scene(narration: str, scene_text: str, issue: str) -> list[str]:
    prompt = f"""
النص الكامل: "{narration}"
جزء المشهد المشكل: "{scene_text}"
المشكلة التي ذكرها المراجع: "{issue}"

اقترح 3 كلمات/جمل بحث بصري إنجليزية جديدة لإيجاد مقطع فيديو أفضل لهذا الجزء.
أعد فقط JSON: {{"alternative_keywords": ["...", "...", "..."]}}
"""
    raw = call_gemini_with_rotation(STAGE, [prompt], response_mime_type="application/json")
    return parse_json_response(raw).get("alternative_keywords", [])


# ملاحظة: حلقة إعادة البناء الكاملة (استدعاء verify_scene_media لكل مشهد فاشل + إعادة الرندر
# الجزئي عبر video_composer.replace_scene_and_rerender + إعادة audit_video) تُنسَّق في main.py
# لأنها تحتاج الوصول لكامل حالة التشغيل (RunState) وقائمة scene_results ومسار الصوت/الترجمة.
MAX_RETRIES = MAX_SCENE_AUDIT_RETRIES
