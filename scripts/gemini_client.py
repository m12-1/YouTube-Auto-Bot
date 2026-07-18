"""
gemini_client.py
غلاف موحد فوق google-genai SDK — يختار المفتاح الصحيح حسب نوع المهمة
(light / advanced / image) بدل ما كل سكربت يكرر نفس منطق الاتصال.
"""
from google import genai
from scripts import config
from scripts.retry_utils import with_backoff

_clients = {}


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
def generate_text(prompt: str, model: str, key_type: str, json_mode: bool = False,
                   temperature: float = 0.9) -> str:
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


@with_backoff(max_retries=4, base_delay=3.0)
def generate_image(prompt: str, model: str = None) -> bytes:
    """يستخدم مفتاح الصور المعزول (GEMINI_KEY_IMAGE) دائماً."""
    client = _get_client("image")
    model = model or config.MODEL_THUMBNAIL
    response = client.models.generate_content(
        model=model,
        contents=prompt,
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return part.inline_data.data
    raise RuntimeError("لم يرجع Gemini أي صورة بالاستجابة")


@with_backoff(max_retries=3, base_delay=2.0)
def get_embedding(text: str, key_type: str = "light") -> list[float]:
    client = _get_client(key_type)
    result = client.models.embed_content(model=config.MODEL_EMBEDDING, contents=text)
    return result.embeddings[0].values
