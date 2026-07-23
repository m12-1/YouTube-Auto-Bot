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

توقيع مكتبة puter-python-sdk الحقيقي (تأكّدنا منه من توثيق المكتبة نفسها على
PyPI/GitHub — راجع https://github.com/CuzImSlymi/puter-python-sdk، وهي نفس
النسخة v0.5.0 المثبَّتة عبر requirements.txt):

    client = PuterAI(username=..., password=...)
    client.login()
    client.set_model("model-name")        # الموديل يُضبط بدالة منفصلة، وليس
                                           # كمعامل model= داخل chat()
    response = client.chat("نص فقط")                       # محادثة نصية
    response = client.chat(prompt="...", images=["path.png"])  # مع صورة
                                           # (مسار ملف محلي أو رابط — وليس
                                           # base64 data URL)
    # response يُرجَع كنص (str) مباشرة، وليس قاموساً فيه مفتاح "message".

هذا بالضبط عكس ما كان مكتوباً سابقاً بهذا الملف (كان يُخمّن عدة أشكال
استدعاء غير صحيحة، منها تمرير model= كوسيط لـ chat() مع تمرير صورة كوسيط
positional ثانٍ — وبما أن الوسيط الثاني الفعلي بتوقيع المكتبة اسمه أيضاً
مرتبط بموضع مشابه، كان هذا يسبب الخطأ المتكرر:
"PuterAI.chat() got multiple values for argument 'model'").
"""
import inspect
import os
import subprocess

from scripts import config, content_policy

_GLOBAL_NEGATIVE_KEYWORDS = ", ".join(sorted(set(content_policy.all_blocked_keywords_flat())))

_ai_client = None

# نفس الموديل المُستخدم بالتحليل البصري الأساسي عبر بنية Puter التحتية.
PUTER_MODEL = "google/gemini-3.5-flash"


class PuterVerificationUnavailable(Exception):
    """تُرفع عندما يتعذّر الحصول على قرار من Puter لأي سبب."""
    pass


def _get_client():
    """تسجيل الدخول وضبط الموديل مرة واحدة فقط (Lazy Singleton) — هذه
    النسخة من المكتبة لا توفر استعادة جلسة محفوظة، فتسجيل الدخول يتكرر مرة
    واحدة فقط لكل تشغيل بالذاكرة (وليس لكل مشهد)."""
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

    ai = PuterAI(username=config.PUTER_USERNAME, password=config.PUTER_PASSWORD)

    try:
        ai.login()
    except Exception as e:
        raise PuterVerificationUnavailable(f"فشل تسجيل الدخول لـ Puter: {e}")

    try:
        ai.set_model(PUTER_MODEL)
    except Exception as e:
        raise PuterVerificationUnavailable(f"فشل ضبط موديل Puter ({PUTER_MODEL}): {e}")

    _assert_chat_signature(ai)

    _ai_client = ai
    print(f"[PUTER] تم تسجيل الدخول بنجاح وضبط الموديل ({PUTER_MODEL}).")
    return _ai_client


def _assert_chat_signature(ai) -> None:
    """فحص تمهيدي (self-test) يتأكد أن `chat()` بالنسخة المثبَّتة فعلياً من
    puter-python-sdk ما زالت تقبل `images=` كما هو موثّق أعلى هذا الملف —
    بدل اكتشاف عدم التطابق فقط بعد فشل كل مشهد بالتشغيل الفعلي (مثال حقيقي
    سابق: SDK غير مثبَّت برقم نسخة محدد → نسخة أحدث غيّرت التوقيع بصمت →
    TypeError متطابق على كل مشهد طوال التشغيل).

    يفحص التوقيع فقط (بلا استدعاء شبكة فعلي) لتفادي استهلاك حصة Puter أو
    تأخير بدء التشغيل — إن لم تدعم النسخة المثبّتة فحص التوقيع لأي سبب
    (Callable غامض مثلاً)، نتجاهل الفحص بصمت بدل إفشال التشغيل بلا داعٍ."""
    try:
        sig = inspect.signature(ai.chat)
    except (ValueError, TypeError):
        return

    params = sig.parameters
    has_images_param = "images" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if not has_images_param:
        raise PuterVerificationUnavailable(
            "توقيع PuterAI.chat() بالنسخة المثبَّتة حالياً لا يقبل معامل "
            "'images' كما هو موثّق (راجع أعلى هذا الملف). على الأرجح تغيّرت "
            "مكتبة puter-python-sdk عن النسخة المثبَّتة بـ requirements.txt. "
            "شغّل `pip show puter-python-sdk` وتأكد من رقم النسخة المثبَّتة "
            "فعلياً، وطابق توقيع chat() الحقيقي مع طريقة الاستدعاء بهذا الملف "
            "قبل المتابعة."
        )


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


def verify_media_relevance(file_path: str, narration: str, topic_context: str = "") -> bool:
    """
    الدالة الرئيسية — نفس توقيع gemini_client.verify_media_relevance
    و groq_client.verify_media_relevance عمداً.

    تحلل الصورة/الفيديو عبر Puter AI وتحكم على تطابقه مع النص السردي.
    لو كان الوسيط فيديو → يُستخرج إطار واحد للتحليل (Puter يقبل مسار ملف
    محلي مباشرة عبر images=[...]، بلا حاجة لتحويله base64 يدوياً).

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

        mood = (topic_context or "").strip()
        mood_clause = (
            f'The overall visual identity/mood of this whole video is: "{mood}". Judge this scene '
            f"WITHIN that context — reject it if it looks out of place for that mood/identity even if "
            f"it superficially matches a single word in the narration. "
            if mood else ""
        )
        negative_clause = (
            f"You MUST answer NO if the image shows any of: {_GLOBAL_NEGATIVE_KEYWORDS}, "
            f"or anything visually unrelated to the video's topic/mood above. "
        )
        prompt = (
            "You are a strict visual quality inspector for a YouTube video. "
            "Does this image clearly and literally show exactly what is described in the narration? "
            + mood_clause + negative_clause +
            "If the narration mentions 'video games', 'digital graphics', or 'pixels', and the image shows a physical board game (like chess or foosball), you MUST answer NO. "
            "If the image is completely unrelated to the core subject of the narration, answer NO. "
            "Answer ONLY with YES or NO.\n\n"
            f"Narration: {narration}"
        )

        try:
            response = ai.chat(prompt=prompt, images=[image_path])
        except Exception as e:
            raise PuterVerificationUnavailable(f"فشل التحليل عبر Puter AI: {e}")

        text = str(response).strip().upper()
        return "YES" in text

    finally:
        if frame_path and os.path.exists(frame_path):
            try:
                os.remove(frame_path)
            except Exception:
                pass
