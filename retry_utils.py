"""
gemini_client.py
مُحدث لعام 2026: يدعم موديلات Gemini 3 و Nano Banana الجديدة.
غلاف موحد فوق google-genai SDK.
"""
from google import genai
from scripts import config
from scripts.retry_utils import with_backoff
from PIL import Image

_clients = {}

# تعريف الموديلات المحدثة لعام 2026
MODEL_TEXT_ADVANCED = "gemini-3.5-flash"  # الموديل الأساسي للمهام البرمجية
MODEL_TEXT_LIGHT = "gemini-3.1-flash-lite" # للمهام السريعة والخفيفة
MODEL_IMAGE_GEN = "gemini-3.1-flash-image"  # الاسم الجديد لـ Nano Banana 2
MODEL_EMBEDDING_NEW = "gemini-embedding-2" # الموديل الجديد الموحد للـ Embeddings

def _get_client(key_type: str) -> genai.Client:
    """key_type: 'light' | 'advanced' | 'image'"""
    if key_type in _clients:
        return _clients[key_type]

    key_map = {
        "light": config.GEMINI_KEY_LIGHT,
        "advanced": config.GEMINI_KEY_ADVANCED,
        "image": config.GEMINI_KEY_IMAGE,
        "filter": config.GEMINI_KEY_FILTER or config.GEMINI_KEY_ADVANCED, # Fallback to advanced if filter key is not provided yet
    }
    api_key = key_map.get(key_type)
    if not api_key:
        raise EnvironmentError(f"مفتاح Gemini المطلوب لـ '{key_type}' غير موجود بالأسرار")

    client = genai.Client(api_key=api_key)
    _clients[key_type] = client
    return client


@with_backoff(max_retries=4, base_delay=3.0)
def _generate_text_internal(prompt: str, model: str, key_type: str, json_mode: bool, temperature: float) -> str:
    """دالة داخلية مع backoff تتولى الطلب الفعلي"""
    client = _get_client(key_type)
    config_kwargs = {"temperature": temperature}
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config_kwargs,
    )
    return response.text


def generate_text(prompt: str, model: str = None, key_type: str = "advanced", json_mode: bool = False,
                  temperature: float = 0.9) -> str:
    """دالة عامة ذكية: إذا استنفدنا حصة الموديل المتقدم، تسقط تلقائياً للموديل الخفيف"""
    target_model = model or (MODEL_TEXT_ADVANCED if key_type == "advanced" else MODEL_TEXT_LIGHT)
    
    try:
        return _generate_text_internal(prompt, target_model, key_type, json_mode, temperature)
    except Exception as e:
        error_str = str(e).lower()
        # إذا نفدت الحصة (429) أو السيرفر مزدحم (503) وكنا نستخدم المفتاح المتقدم، نلجأ للخفيف
        if ("429" in error_str or "503" in error_str) and key_type == "advanced" and config.GEMINI_KEY_LIGHT:
            reason = "نفاد الحصة (429)" if "429" in error_str else "ازدحام السيرفر (503)"
            print(f"[GEMINI FALLBACK] {reason} على الموديل المتقدم. الانتقال للموديل الخفيف ({MODEL_TEXT_LIGHT})...")
            return _generate_text_internal(prompt, MODEL_TEXT_LIGHT, "light", json_mode, temperature)
        raise e


@with_backoff(max_retries=4, base_delay=3.0)
def generate_image(prompt: str, model: str = None) -> bytes:
    """يستخدم مفتاح الصور وموديل Nano Banana 2 الجديد."""
    client = _get_client("image")
    target_model = model or MODEL_IMAGE_GEN
    response = client.models.generate_content(
        model=target_model,
        contents=prompt,
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return part.inline_data.data
    raise RuntimeError("لم يرجع Gemini أي صورة بالاستجابة")


def _verify_media_internal(image_path: str, narration: str, model: str, key_type: str) -> bool:
    client = _get_client(key_type)
    prompt = (
        "You are a strict visual quality inspector for a YouTube video. "
        "Does this image clearly and literally show exactly what is described in the narration? "
        "If the narration mentions 'video games', 'digital graphics', or 'pixels', and the image shows a physical board game (like chess or foosball), you MUST answer NO. "
        "If the image is completely unrelated to the core subject of the narration, answer NO. "
        f"Answer ONLY with YES or NO.\n\nNarration: {narration}"
    )
    
    with Image.open(image_path) as img:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, img],
            config={"temperature": 0.0} # Strict deterministic
        )
        text = response.text.strip().upper()
        return "YES" in text

def verify_media_relevance(image_path: str, narration: str) -> bool:
    """
    تتحقق ما إذا كانت الصورة أو إطار الفيديو يتطابق مع نص السرد (التحقق البصري)
    تستخدم سلسلة من 4 نماذج لتفادي نفاد الحصة (429) على الطبقة المجانية.
    """
    models_to_try = [
        (MODEL_TEXT_ADVANCED, "filter"),        # gemini-3.5-flash (أفضل جودة)
        (MODEL_TEXT_LIGHT, "filter"),           # gemini-3.1-flash-lite (سريع وخفيف)
        ("gemini-2.5-flash", "filter"),         # الجيل السابق (قوي)
        ("gemini-2.5-flash-lite", "filter"),    # الجيل السابق (خفيف)
    ]
    
    for i, (model_name, key_type) in enumerate(models_to_try):
        try:
            return _verify_media_internal(image_path, narration, model_name, key_type)
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "503" in error_str or "quota" in error_str:
                if i < len(models_to_try) - 1:
                    next_model = models_to_try[i+1][0]
                    print(f"[GEMINI CASCADE] الموديل {model_name} غير متاح ({'429' if '429' in error_str else '503'}). الانتقال فوراً إلى {next_model}...")
                    continue # Try the next model
            
            # If it's a completely different error, or we ran out of models
            print(f"[GEMINI ERROR] فشل التحقق البصري باستخدام كل النماذج المتاحة: {e}")
            break # Break the loop and fallback to True
            
    print("[ASSET WARNING] تم تخطي الفلتر البصري بسبب تعطل جميع النماذج لتجنب توقف الإنتاج.")
    return True # Fallback to True if vision check completely fails


@with_backoff(max_retries=3, base_delay=2.0)
def get_embedding(text: str, key_type: str = "light") -> list[float]:
    """يستخدم الموديل الجديد الموحد Gemini Embedding 2."""
    client = _get_client(key_type)
    result = client.models.embed_content(model=MODEL_EMBEDDING_NEW, contents=text)
    return result.embeddings[0].values
