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
import os
import subprocess
import tempfile

from shared.gemini_client import call_gemini_with_rotation, parse_json_response
from config import SCENE_AUDIT_ACCEPT_THRESHOLD, MAX_SCENE_AUDIT_RETRIES

logger = logging.getLogger("modules.final_scene_audit")

STAGE = "final_scene_audit"

# نضغط الفيديو النهائي قبل إرساله لـ Gemini للتدقيق، لأن الفيديو الكامل بجودة
# التصدير (1080x1920) يتجاوز غالبًا حد الـ inline data لدى Gemini (~18MB)،
# تمامًا كما في مرحلة التحقق من الوسائط (ai_media_verification).
_AUDIT_PREVIEW_SCALE_HEIGHT = 480
_AUDIT_PREVIEW_VIDEO_BITRATE = "500k"
_AUDIT_PREVIEW_AUDIO_BITRATE = "64k"

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


def _make_audit_preview(source_path: str) -> str:
    """
    يولّد نسخة مضغوطة (دقة/بترييت أقل) من الفيديو النهائي لإرسالها لـ Gemini
    ضمن حد الـ inline data، مع الإبقاء على الصوت (بترييت منخفض) لأن التدقيق
    قد يستفيد من مشاهدة الفيديو كاملًا بصوته. يعيد المسار الأصلي إن فشل الضغط
    لأي سبب (ffmpeg غير متاح، ...)، فيتولى استدعاء call_gemini_with_rotation
    التعامل مع الخطأ إن كان الملف الأصلي ما زال كبيرًا جدًا.
    """
    out_path = os.path.join(tempfile.gettempdir(), f"audit_preview_{os.getpid()}.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-vf", f"scale=-2:{_AUDIT_PREVIEW_SCALE_HEIGHT}",
        "-b:v", _AUDIT_PREVIEW_VIDEO_BITRATE,
        "-b:a", _AUDIT_PREVIEW_AUDIO_BITRATE,
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        subprocess.run(
            cmd, check=True, timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("فشل توليد معاينة مضغوطة للفيديو النهائي (%s)، سيُستخدم الملف الأصلي.", e)
        return source_path

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.warning("معاينة التدقيق فارغة/غير موجودة، سيُستخدم الملف الأصلي.")
        return source_path
    return out_path


def audit_video(final_video_path: str, narration: str, scenes: list[dict]) -> list[dict]:
    scene_list_str = "\n".join(f"- {s['id']}: {s['text']}" for s in scenes)
    prompt = _AUDIT_PROMPT.format(narration=narration, scene_list=scene_list_str)
    preview_path = _make_audit_preview(final_video_path)
    raw = call_gemini_with_rotation(
        STAGE, [prompt], media_paths=[preview_path], response_mime_type="application/json"
    )
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
