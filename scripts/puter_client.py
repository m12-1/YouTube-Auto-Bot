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

    ai = PuterAI()

    # محاولة استعادة جلسة محفوظة أولاً
    if os.path.exists(_SESSION_FILE):
        try:
            with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            ai.restore_session(session_data)
            _ai_client = ai
            print("[PUTER] تم استعادة الجلسة المحفوظة بنجاح.")
            return _ai_client
        except Exception:
            # الجلسة منتهية أو تالفة — تسجيل دخول جديد
            pass

    # تسجيل دخول جديد
    try:
        ai.login(username=config.PUTER_USERNAME, password=config.PUTER_PASSWORD)
    except Exception as e:
        raise PuterVerificationUnavailable(f"فشل تسجيل الدخول لـ Puter: {e}")

    # حفظ الجلسة
    try:
        session_data = ai.get_session()
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(session_data, f)
    except Exception:
        pass  # فشل الحفظ لا يوقف العمل

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
            response = ai.chat(
                prompt,
                media=[image_data_url],
                model="google/gemini-3.5-flash",
            )
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
