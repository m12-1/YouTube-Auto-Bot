"""
media_relevance_checker.py
مهمة هذا الملف فقط: التحقق من "تطابق المشاهد" — هل الصورة/الفيديو المختار
لمشهد ما يطابق فعلاً النص السردي (narration) الخاص به؟

الطبقة الأولى (كما هي بالمشروع الأصلي بدون أي تغيير بالمنطق): إرسال الوسيط
إلى Gemini Vision عبر gemini_client.verify_media_relevance، والتي تجرب
سلسلة من 4 نماذج قبل الاستسلام.

الطبقة الثانية (جديدة): لو فشلت *كل* نماذج Gemini لأي سبب (نفاد حصة، ازدحام
سيرفر، انقطاع اتصال...الخ)، يتم تفعيل Groq عبر groq_client.verify_media_relevance
(نموذج qwen/qwen3.6-27b متعدد الوسائط) كطبقة احتياطية ثانية أقوى دلالياً من
CLIP — لو كان الوسيط فيديو، تُستخرج عدة إطارات منه كل 5 ثوانٍ بدل إطار واحد
فقط، ليحكم النموذج على المقطع ككل.

الطبقة الثالثة (جديدة — الحل المقترح سابقاً): لو فشل Groq أيضاً لأي سبب،
يتم تفعيل "حارس الجودة المحلي" القائم على CLIP (Contrastive Language-Image
Pre-training)، وهو نموذج مفتوح المصدر يعمل محلياً بدون أي اتصال إنترنت أو
مفتاح API، ويقيس درجة التشابه الدلالي بين الوسيط والنص مباشرة.

الطبقة الرابعة (نفس سلوك المشروع الأصلي): لو تعطل حتى CLIP لأي سبب (فشل
تحميل النموذج، ملف تالف...الخ)، نوافق تلقائياً على الوسيط لتفادي توقف
الإنتاج بالكامل بسبب عطل بخطوة الفحص نفسها فقط (وليس بسبب الوسيط فعلاً).
"""
import os
import subprocess

from scripts import gemini_client
from scripts import groq_client

# عتبة قرار CLIP: أي تشابه cosine بين الصورة والنص أعلى منها يُعتبر "متطابق".
# 0.20-0.22 قيمة معتدلة شائعة الاستخدام مع نموذج ViT-B-32 لتفادي رفض مبالغ فيه.
CLIP_SIMILARITY_THRESHOLD = 0.20
CLIP_MODEL_NAME = "ViT-B-32"
CLIP_PRETRAINED = "openai"

_clip_model = None
_clip_preprocess = None
_clip_tokenizer = None


def _load_clip():
    """تحميل نموذج CLIP مرة واحدة فقط (Lazy Singleton) — لا يُحمَّل إطلاقاً
    إلا إذا فشلت كل نماذج Gemini فعلاً، لتفادي إبطاء كل تشغيل عادي ناجح."""
    global _clip_model, _clip_preprocess, _clip_tokenizer
    if _clip_model is not None:
        return _clip_model, _clip_preprocess, _clip_tokenizer

    import open_clip  # استيراد مؤجل: يحتاج torch + open_clip_torch (راجع requirements.txt)

    print(f"[CLIP] تحميل نموذج {CLIP_MODEL_NAME} ({CLIP_PRETRAINED}) لأول مرة محلياً "
          f"— قد يأخذ وقتاً إضافياً بأول استدعاء فقط...")
    model, _, preprocess = open_clip.create_model_and_transforms(CLIP_MODEL_NAME, pretrained=CLIP_PRETRAINED)
    tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
    model.eval()

    _clip_model, _clip_preprocess, _clip_tokenizer = model, preprocess, tokenizer
    return model, preprocess, tokenizer


def _clip_check(image_path: str, narration: str) -> bool:
    """يقيس درجة التشابه الدلالي بين الصورة والنص عبر CLIP محلياً، ويرجع
    True لو كانت الدرجة أعلى من العتبة المقبولة."""
    import torch
    from PIL import Image

    model, preprocess, tokenizer = _load_clip()

    text = (narration or "").strip()[:300] or "a relevant photo"
    with Image.open(image_path) as img:
        image_input = preprocess(img.convert("RGB")).unsqueeze(0)
    text_input = tokenizer([text])

    with torch.no_grad():
        image_features = model.encode_image(image_input)
        text_features = model.encode_text(text_input)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        similarity = (image_features @ text_features.T).item()

    print(f"[CLIP] درجة التشابه بين الوسيط والنص: {similarity:.3f} (عتبة القبول: {CLIP_SIMILARITY_THRESHOLD})")
    return similarity >= CLIP_SIMILARITY_THRESHOLD


def verify_media_file(file_path: str, narration: str) -> bool:
    """
    الدالة الرئيسية المستخدمة بباقي المشروع — نفس الاسم والتوقيع اللذين
    كانا سابقاً بـ asset_fetcher.py، لضمان استبدال سلس بدون كسر أي استدعاء.

    الخطوات:
    1) لو الملف فيديو، استخراج إطار من الثانية 0.5 للتحقق عبر Gemini/CLIP
       (نفس منطق سابق — هذان الاثنان يفحصان لقطة واحدة فقط).
    2) محاولة Gemini Vision (سلسلة النماذج الأربعة كما بالأصل تماماً).
    3) لو فشلت كل نماذج Gemini -> تفعيل Groq (يفحص الفيديو بعدة إطارات كل
       5 ثوانٍ من الملف الأصلي مباشرة، وليس اللقطة الواحدة من الخطوة 1).
    4) لو فشل Groq أيضاً -> تفعيل حارس الجودة المحلي CLIP (على نفس اللقطة
       الواحدة من الخطوة 1).
    5) لو فشل CLIP أيضاً -> قبول تلقائي (نفس سلوك الأصل: عطل بالفحص نفسه
       لا يوقف الإنتاج).
    """
    check_path = file_path
    is_temp = False

    if file_path.lower().endswith(('.mp4', '.webm', '.mov')):
        thumb_path = file_path + "_thumb.jpg"
        try:
            cmd = [
                "ffmpeg", "-y", "-i", file_path,
                "-ss", "00:00:00.500", "-vframes", "1",
                "-q:v", "5", thumb_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            check_path = thumb_path
            is_temp = True
        except Exception as e:
            print(f"[MEDIA CHECK WARNING] فشل استخراج صورة من الفيديو للتحقق ({e})، سيتم افتراض نجاح التحقق.")
            return True

    try:
        try:
            is_relevant = gemini_client.verify_media_relevance(check_path, narration)
            if not is_relevant:
                print(f"[MEDIA CHECK FILTER] Gemini رفض الوسائط. لا تطابق مع: {narration[:50]}...")
            return is_relevant
        except gemini_client.GeminiVerificationUnavailable as e:
            print(f"[MEDIA CHECK WARNING] تعطلت كل نماذج Gemini للتحقق البصري ({e}). "
                  f"جاري تفعيل Groq كطبقة حماية ثانية...")
        except Exception as e:
            # أي خطأ غير متوقع آخر (مثلاً تلف بملف الصورة نفسه) — نطبّق نفس
            # منطق الاحتياط: نجرب Groq قبل الانتقال لـ CLIP
            print(f"[MEDIA CHECK WARNING] خطأ غير متوقع أثناء التحقق عبر Gemini ({e}). تجربة Groq...")

        try:
            # نمرر file_path الأصلي (وليس check_path اللقطة الواحدة) لأن
            # groq_client يتولى بنفسه استخراج عدة إطارات كل 5 ثوانٍ لو كان
            # الوسيط فيديو، بدل الاكتفاء بلقطة واحدة من الثانية 0.5.
            is_relevant = groq_client.verify_media_relevance(file_path, narration)
            if not is_relevant:
                print(f"[MEDIA CHECK FILTER] Groq رفض الوسائط. لا تطابق مع: {narration[:50]}...")
            return is_relevant
        except groq_client.GroqVerificationUnavailable as e:
            print(f"[MEDIA CHECK WARNING] تعطل Groq أيضاً ({e}). "
                  f"جاري تفعيل حارس الجودة المحلي CLIP كطبقة حماية ثالثة وأخيرة...")
        except Exception as e:
            print(f"[MEDIA CHECK WARNING] خطأ غير متوقع أثناء التحقق عبر Groq ({e}). تجربة CLIP...")

        try:
            is_relevant = _clip_check(check_path, narration)
            if not is_relevant:
                print(f"[MEDIA CHECK FILTER] CLIP (الحارس المحلي) رفض الوسائط لعدم تطابقها مع: {narration[:50]}...")
            return is_relevant
        except Exception as e:
            print(f"[MEDIA CHECK WARNING] فشل حارس الجودة المحلي CLIP أيضاً ({e})، "
                  f"سيتم افتراض نجاح التحقق لتجنب توقف الإنتاج بالكامل.")
            return True
    finally:
        if is_temp and os.path.exists(check_path):
            try:
                os.remove(check_path)
            except Exception:
                pass
