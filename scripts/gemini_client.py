"""
gemini_client.py
مُحدث لعام 2026: يدعم موديلات Gemini 3 و Nano Banana الجديدة.
غلاف موحد فوق google-genai SDK.
"""
from google import genai
from scripts import config
from scripts.retry_utils import with_backoff

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


@with_backoff(max_retries=3, base_delay=2.0)
def get_embedding(text: str, key_type: str = "light") -> list[float]:
    """يستخدم الموديل الجديد الموحد Gemini Embedding 2."""
    client = _get_client(key_type)
    result = client.models.embed_content(model=MODEL_EMBEDDING_NEW, contents=text)
    return result.embeddings[0].values
