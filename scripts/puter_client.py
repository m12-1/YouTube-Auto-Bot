"""
puter_client.py
طبقة تحليل بصري ثالثة (بعد Groq وقبل CLIP) — تستخدم Puter AI كوسيط
للوصول لنماذج مجانية (google/gemini-3.5-flash عبر بنيتهم التحتية) بحصة
منفصلة تماماً عن مفاتيح Gemini الخاصة بك.

المميزات:
- حساب مجاني بالكامل (puter.com)
- حصة منفصلة عن Google AI Studio
- يدعم تحليل الصور مباشرة
- جلسة مستمرة (session persistence) لتجنب تسجيل الدخول المتكرر

ملاحظة عن الفيديو:
Puter AI (مثل Groq) لا يقبل ملفات فيديو مباشرة. لو كان الوسيط فيديو،
نستخرج إطاراً واحداً من الثانية 0.5 عبر ffmpeg ونرسل الصورة للتحليل.

أين تضع الإيميل والرمز:
- في GitHub Secrets أضف: PUTER_USERNAME (الإيميل) و PUTER_PASSWORD (كلمة المرور)
- للتشغيل المحلي: في ملف .env أضف نفس المتغيرين
"""
import base64
import json
import os
import subprocess
import tempfile

from scripts import config

_ai_client = None
_SESSION_FILE = os.path.join(tempfile.gettempdir(), "puter_session.json")


class PuterVerificationUnavailable(Exception):
    """تُرفع عندما يتعذّر الحصول على قرار من Puter لأي سبب."""
    pass


def _get_client():
    """تسجيل الدخول أو استعادة الجلسة المحفوظة."""
    global _ai_client
    if _ai_client is not None:
        return _ai_client

    if not config.PUTER_USERNAME or not config.PUTER_PASSWORD:
        raise PuterVerificationUnavailable(
            "بيانات Puter غير موجودة (PUTER_USERNAME / PUTER_PASSWORD). "
            "راجع SECRETS.md."
        )

    try:
        from puter import PuterAI
    except ImportError:
        raise PuterVerificationUnavailable(
            "مكتبة puter-python-sdk غير مثبتة. شغّل: pip install puter-python-sdk"
        )

    # ملاحظة مهمة: puter-python-sdk (v0.5.0) يستقبل username/password في
    # مُنشئ الكلاس PuterAI(...) وليس في login() — login() لا يقبل أي
    # معاملات إطلاقاً (كان هذا سبب الفشل المتكرر "unexpected keyword
    # argument 'username'" سابقاً، والذي كان يهدر 3 محاولات × 60 ثانية
    # انتظار على كل مشهد بلا أي فائدة).
    ai = PuterAI(username=config.PUTER_USERNAME, password=config.PUTER_PASSWORD)

    # ملاحظة: هذا الإصدار من المكتبة لا يوفر get_session()/restore_session()
    # إطلاقاً، فحفظ/استعادة الجلسة معطّل حالياً (تسجيل دخول جديد كل تشغيل،
    # وهذا مقبول لأنه أسرع بكثير من الفشل المتكرر السابق).
    try:
        ai.login()
    except Exception as e:
        raise PuterVerificationUnavailable(f"فشل تسجيل الدخول لـ Puter: {e}")

    _ai_client = ai
    print("[PUTER] تم تسجيل الدخول بنجاح.")
    return _ai_client


def _extract_single_frame(video_path: str) -> str:
    """يستخرج إطاراً واحداً من الثانية 0.5 من الفيديو عبر ffmpeg."""
    tmp_path = video_path + "_puter_frame.jpg"
    try:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ss", "00:00:00.500", "-vframes", "1",
            "-q:v", "5", tmp_path,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return tmp_path
    except Exception as e:
        raise PuterVerificationUnavailable(f"فشل استخراج إطار من الفيديو: {e}")


def _encode_image_base64(image_path: str) -> str:
    """يقرأ الصورة ويحولها لـ base64 data URL."""
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{ext};base64,{b64}"


# إصلاح جذري لخطأ "PuterAI.chat() got an unexpected keyword argument 'media'"
# الذي كان يظهر في كل مشهد بلا استثناء بالسجل: الكود كان يفترض أن chat()
# تقبل media=[...] لكن هذا لا يتطابق مع توقيع الدالة الفعلي بالمكتبة
# المثبَّتة. بما أنه لا يوجد وصول للإنترنت هنا للتأكد من التوقيع الدقيق
# لإصدارك من puter-python-sdk، هذه الدالة تجرّب عدة أشكال استدعاء معروفة/
# شائعة بدل الاعتماد على اسم واحد فقط، وتحفظ أول شكل ينجح لتفادي إعادة
# المحاولة في كل مشهد لاحق.
#
# ⚠️ ملاحظة لك: لو استمر ظهور هذا الخطأ بعد هذا التعديل، يعني أن مكتبتك لا
# تدعم أياً من الأشكال أدناه — افتح بيئتك المحلية ونفّذ:
#   python -c "import inspect, puter; print(inspect.signature(puter.PuterAI.chat))"
# وأرسل لي الناتج لأضبط الاسم الصحيح بدقة.
_puter_chat_kwarg = None  # يُحفظ هنا أول اسم معامل ينجح خلال هذا التشغيل


def _call_chat_compat(ai, prompt: str, image_data_url: str) -> dict:
    global _puter_chat_kwarg

    if _puter_chat_kwarg == "__positional__":
        return ai.chat(prompt, image_data_url, model="google/gemini-3.5-flash")

    candidates = []
    if _puter_chat_kwarg is not None:
        candidates.append(_puter_chat_kwarg)
    candidates += [c for c in ("media", "images", "image", "attachments", "files") if c not in candidates]

    last_error = None
    for kwarg_name in candidates:
        try:
            response = ai.chat(prompt, model="google/gemini-3.5-flash", **{kwarg_name: [image_data_url]})
            _puter_chat_kwarg = kwarg_name
            return response
        except TypeError as e:
            last_error = e
            continue

    # آخر محاولة: تمرير رابط الصورة كوسيط positional بعد الـ prompt (شكل
    # شائع آخر: chat(prompt, image_url, model=...))
    try:
        response = ai.chat(prompt, image_data_url, model="google/gemini-3.5-flash")
        _puter_chat_kwarg = "__positional__"
        return response
    except TypeError as e:
        last_error = e

    raise TypeError(
        f"لم ينجح أي شكل استدعاء معروف لـ PuterAI.chat() مع هذه المكتبة "
        f"(آخر خطأ: {last_error}). راجع الملاحظة أعلى الدالة لمعرفة التوقيع الصحيح."
    )


def verify_media_relevance(file_path: str, narration: str) -> bool:
    """
    الدالة الرئيسية — نفس توقيع gemini_client.verify_media_relevance
    و groq_client.verify_media_relevance عمداً.

    تحلل الصورة/الفيديو عبر Puter AI وتحكم على تطابقه مع النص السردي.
    لو كان الوسيط فيديو → يُستخرج إطار واحد للتحليل.

    ترفع PuterVerificationUnavailable لو تعذر الوصول لـ Puter.
    """
    ai = _get_client()
    is_video = file_path.lower().endswith((".mp4", ".webm", ".mov"))
    frame_path = None

    try:
        if is_video:
            frame_path = _extract_single_frame(file_path)
            image_path = frame_path
        else:
            image_path = file_path

        image_data_url = _encode_image_base64(image_path)

        prompt = (
            "You are a strict visual quality inspector for a YouTube video. "
            "Does this image clearly and literally show exactly what is described in the narration? "
            "If the narration mentions 'video games', 'digital graphics', or 'pixels', and the image shows a physical board game (like chess or foosball), you MUST answer NO. "
            "If the image is completely unrelated to the core subject of the narration, answer NO. "
            "Answer ONLY with YES or NO.\n\n"
            f"Narration: {narration}"
        )

        try:
            response = _call_chat_compat(ai, prompt, image_data_url)
            text = (response.get("message", "") or "").strip().upper()
            return "YES" in text
        except Exception as e:
            raise PuterVerificationUnavailable(f"فشل التحليل عبر Puter AI: {e}")

    finally:
        if frame_path and os.path.exists(frame_path):
            try:
                os.remove(frame_path)
            except Exception:
                pass
