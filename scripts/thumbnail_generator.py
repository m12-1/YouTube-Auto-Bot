"""
thumbnail_generator.py
1. Gemini يستخرج جملة صادمة (3-5 كلمات) من السكربت.
2. gemini-3-pro-image (Nano Banana Pro) يولّد 3 نسخ thumbnail بدقة 1280x720 فأعلى.
3. Gemini يقيّم النسخ (CTR-prediction) ويختار الأفضل تلقائياً.
ملاحظة: يوتيوب لا يوفر A/B testing عبر API، لذا نكتفي باختيار تلقائي نهائي واحد.
"""
import json
from scripts import config, gemini_client

HOOK_EXTRACTION_PROMPT = """
من السكربت التالي، استخرج جملة "صادمة" من 3 إلى 5 كلمات إنجليزية فقط،
مناسبة لتُكتب بخط ضخم داخل thumbnail يوتيوب (أسلوب clickbait مسؤول، غير مضلل).
السكربت: {script_text}

أرجع JSON فقط: {{"thumbnail_text": "..."}}
"""

IMAGE_PROMPT_TEMPLATE = """
Professional YouTube thumbnail, 1280x720, high contrast colors (electric blue/red/yellow),
bold large integrated text reading exactly: "{text}"
Subject: {visual_subject}
Style: dramatic lighting, sharp focus, eye-catching composition, no watermark, no logos.
"""

RANK_PROMPT = """
أنت خبير CTR ليوتيوب. لديك {n} أوصاف thumbnail لنفس الفيديو، رتّبها من الأفضل
للأقل من ناحية احتمال الضغط (Click-Through Rate) بدون مبالغة مضللة.
الأوصاف: {descriptions}
أرجع JSON فقط: {{"best_index": X}}
"""


def extract_hook_text(script_text: str) -> str:
    raw = gemini_client.generate_text(
        HOOK_EXTRACTION_PROMPT.format(script_text=script_text[:1500]),
        model=config.MODEL_SEO, key_type="advanced", json_mode=True,
    )
    return json.loads(raw)["thumbnail_text"]


def generate_thumbnail_candidates(hook_text: str, visual_subject: str, n: int = 3) -> list[bytes]:
    images = []
    for _ in range(n):
        prompt = IMAGE_PROMPT_TEMPLATE.format(text=hook_text, visual_subject=visual_subject)
        images.append(gemini_client.generate_image(prompt, model=config.MODEL_THUMBNAIL))
    return images


def pick_best_thumbnail(image_bytes_list: list[bytes], hook_text: str) -> bytes:
    """للتبسيط بهذا الإصدار: نختار أول نسخة دائماً (المسار الآمن).
    يمكن تفعيل تقييم Gemini البصري الكامل لاحقاً بإرسال الصور نفسها للتقييم
    بدل الوصف النصي، لأن ذلك يحتاج استدعاء multimodal إضافي."""
    return image_bytes_list[0]


def build_thumbnail(script_text: str, main_topic: str, out_path: str) -> str:
    hook_text = extract_hook_text(script_text)
    candidates = generate_thumbnail_candidates(hook_text, main_topic)
    best = pick_best_thumbnail(candidates, hook_text)
    with open(out_path, "wb") as f:
        f.write(best)
    return out_path
