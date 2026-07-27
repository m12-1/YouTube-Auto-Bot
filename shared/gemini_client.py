"""
نظام تدوير مفاتيح ونماذج Gemini.

لكل مرحلة (stage):
  1. يبدأ بالمفتاح المخصص لها (STAGE_KEY_MAP) + أول نموذج في MODEL_CHAIN.
  2. عند 429 (نفاد حصة): ينتقل للنموذج التالي على نفس المفتاح.
  3. إذا نفدت كل النماذج على هذا المفتاح: ينتقل للمفتاح التالي في ALL_KEYS_ORDER
     (بدءًا من أول نموذج من جديد)، حتى تجربة كل المفاتيح × كل النماذج.
  4. مفتاح غير موجود في البيئة (فارغ) يُتخطى فورًا دون استهلاك محاولة فعلية.
"""

import os
import json
import time
import logging

import google.generativeai as genai

from config import MODEL_CHAIN, STAGE_KEY_MAP, ALL_KEYS_ORDER

logger = logging.getLogger("shared.gemini_client")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class RateLimitError(Exception):
    """تُرفع عند استلام 429 / ResourceExhausted من Gemini."""


class TransientError(Exception):
    """أخطاء مؤقتة (timeout / 5xx) يستحق إعادة نفس المحاولة."""


class AllAttemptsExhaustedError(Exception):
    """فشلت كل تركيبات المفاتيح × النماذج."""


class GeminiKeyModelRotator:
    def __init__(self, stage_name: str):
        self.stage_name = stage_name
        self.primary_key_name = STAGE_KEY_MAP.get(stage_name, ALL_KEYS_ORDER[0])
        # المفتاح الأساسي أولاً، ثم بقية المفاتيح بالدور (لا مفتاح عاطل)
        self.key_order = [self.primary_key_name] + [
            k for k in ALL_KEYS_ORDER if k != self.primary_key_name
        ]
        self._key_index = 0
        self._model_index = 0

    @property
    def current_key_name(self) -> str:
        return self.key_order[self._key_index]

    @property
    def current_key_value(self):
        return os.environ.get(self.current_key_name)

    @property
    def current_model(self) -> str:
        return MODEL_CHAIN[self._model_index]

    def advance(self) -> bool:
        """ينتقل للمحاولة التالية. يعيد False إن نفدت كل المحاولات."""
        self._model_index += 1
        if self._model_index >= len(MODEL_CHAIN):
            self._model_index = 0
            self._key_index += 1
            if self._key_index >= len(self.key_order):
                return False
            logger.warning(
                "[%s] نفدت كل نماذج المفتاح %s، الانتقال إلى %s",
                self.stage_name, self.key_order[self._key_index - 1], self.current_key_name,
            )
        return True

    def reset(self):
        self._key_index = 0
        self._model_index = 0


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "resource_exhausted" in msg or "quota" in msg


def _looks_like_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(code in msg for code in ["500", "502", "503", "504", "timeout", "deadline"])


def call_gemini_with_rotation(stage_name: str, prompt_parts: list, response_mime_type: str = None,
                               max_output_tokens: int = 2048, temperature: float = 0.7):
    """
    يستدعي Gemini مع تدوير تلقائي بين المفاتيح والنماذج عند 429.

    prompt_parts: قائمة أجزاء المحتوى (نص و/أو ملفات مرفوعة عبر genai.upload_file)
    response_mime_type: مثلاً "application/json" لإجبار خرج JSON منظم
    يعيد: نص الاستجابة (str)
    """
    rotator = GeminiKeyModelRotator(stage_name)
    max_attempts = len(ALL_KEYS_ORDER) * len(MODEL_CHAIN)
    last_error = None

    for attempt in range(max_attempts):
        key_val = rotator.current_key_value
        model_name = rotator.current_model

        if not key_val:
            logger.info("المفتاح %s غير موجود في البيئة، تخطي.", rotator.current_key_name)
            if not rotator.advance():
                break
            continue

        try:
            logger.info(
                "[%s] محاولة %d/%d -> key=%s model=%s",
                stage_name, attempt + 1, max_attempts, rotator.current_key_name, model_name,
            )
            genai.configure(api_key=key_val)

            generation_config = {
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            }
            if response_mime_type:
                generation_config["response_mime_type"] = response_mime_type

            model = genai.GenerativeModel(model_name, generation_config=generation_config)
            response = model.generate_content(prompt_parts)

            if not response.candidates:
                raise TransientError("لا يوجد candidates في الاستجابة")

            return response.text

        except Exception as e:  # noqa: BLE001 - نحتاج التقاط أي استثناء من SDK لتصنيفه
            last_error = e
            if _looks_like_rate_limit(e):
                logger.warning(
                    "[%s] 429 على %s/%s — الانتقال للتالي.",
                    stage_name, rotator.current_key_name, model_name,
                )
                if not rotator.advance():
                    break
                time.sleep(1.0)
                continue
            if _looks_like_transient(e):
                logger.warning("[%s] خطأ مؤقت: %s — إعادة المحاولة بنفس الإعداد.", stage_name, e)
                time.sleep(2.0)
                continue
            # خطأ غير متوقع (مثلاً prompt مرفوض) — لا فائدة من إعادة نفس التركيبة، ننتقل للتالي
            logger.error("[%s] خطأ غير متوقع: %s", stage_name, e)
            if not rotator.advance():
                break
            continue

    raise AllAttemptsExhaustedError(
        f"[{stage_name}] فشلت كل محاولات Gemini ({max_attempts}) عبر كل المفاتيح والنماذج. "
        f"آخر خطأ: {last_error}"
    )


def upload_media_file(path: str):
    """يرفع ملف وسائط (فيديو/صورة) إلى Gemini Files API لاستخدامه في التحقق البصري."""
    return genai.upload_file(path)


def parse_json_response(raw_text: str) -> dict:
    """يزيل أسوار Markdown المحتملة (```json ... ```) ثم يحلل JSON."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())
