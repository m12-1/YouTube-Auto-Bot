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
import mimetypes

from google import genai
from google.genai import types

from config import MODEL_CHAIN, STAGE_KEY_MAP, ALL_KEYS_ORDER

logger = logging.getLogger("shared.gemini_client")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# الحد الأقصى التقريبي لحجم بيانات الوسائط inline في طلب واحد لـ Gemini
# (يتجاوز هذا الحد يتطلب Files API، لكنها معطّلة تمامًا في هذه الحزمة المتوقفة).
_MAX_INLINE_MEDIA_BYTES = 18 * 1024 * 1024


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


def _load_inline_media_part(path: str) -> dict:
    """
    يقرأ ملف الوسائط ويعيده كجزء inline_data (bytes + mime_type) يُرسَل مباشرة
    ضمن طلب generate_content، دون المرور عبر Files API (genai.upload_file).

    السبب: حزمة google.generativeai أوقفت جوجل دعمها بالكامل، ومسار Files API
    فيها بات يفشل بخطأ "API key not valid" مع كل مفتاح دون استثناء (بينما
    generate_content النصي/متعدد الوسائط عبر inline_data ما زال يعمل). لذلك
    نتجنب upload_file/get_file كليًا ونمرر بيانات الفيديو مباشرة كـ inline.
    """
    size = os.path.getsize(path)
    if size > _MAX_INLINE_MEDIA_BYTES:
        raise TransientError(
            f"ملف {path} كبير جدًا ({size} بايت) لإرساله كـ inline data "
            f"(الحد التقريبي {_MAX_INLINE_MEDIA_BYTES} بايت)."
        )
    mime_type = mimetypes.guess_type(path)[0] or "video/mp4"
    with open(path, "rb") as f:
        data = f.read()
    return types.Part.from_bytes(data=data, mime_type=mime_type)


def call_gemini_with_rotation(stage_name: str, text_parts: list, media_paths: list | None = None,
                               response_mime_type: str = None, response_schema=None,
                               max_output_tokens: int = 2048, temperature: float = 0.7):
    """
    يستدعي Gemini مع تدوير تلقائي بين المفاتيح والنماذج عند 429.

    text_parts: قائمة أجزاء نصية (prompts)
    media_paths: مسارات ملفات وسائط محلية (فيديو/صورة) تُرسَل كـ inline data مباشرة
                 ضمن كل طلب (بدل Files API المعطّلة في هذه الحزمة المتوقفة).
    response_mime_type: مثلاً "application/json" لإجبار خرج JSON منظم
    response_schema: (اختياري) types.Schema أو dict يصف بنية الـ JSON المطلوبة بدقة.
                 عند تمريره مع response_mime_type="application/json" يفرض Gemini فعليًا
                 الالتزام بالبنية (structured output)، بدل الاعتماد فقط على وصف البنية
                 داخل نص البرومبت — يمنع الغالبية العظمى من أخطاء "JSON غير سليم".
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
            client = genai.Client(api_key=key_val)

            # يمرر ملفات الوسائط كـ inline data (بدل رفعها عبر Files API المعطّلة)
            inline_media = [_load_inline_media_part(p) for p in (media_paths or [])]
            prompt_parts = list(text_parts) + inline_media

            config_kwargs = {
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            }
            if response_mime_type:
                config_kwargs["response_mime_type"] = response_mime_type
            if response_schema is not None:
                config_kwargs["response_schema"] = response_schema

            response = client.models.generate_content(
                model=model_name,
                contents=prompt_parts,
                config=types.GenerateContentConfig(**config_kwargs),
            )

            if not response.candidates:
                raise TransientError("لا يوجد candidates في الاستجابة")

            response_text = response.text
            if not response_text or not str(response_text).strip():
                # يحدث هذا مثلاً عند finish_reason=MALFORMED_RESPONSE أو أي حالة
                # يعيد فيها SDK كائن استجابة صالحًا (candidates موجودة) لكن بلا
                # نص فعلي (response.text = None). سابقًا كان هذا يُعامَل كنجاح
                # فيُعاد None للمستدعي، الذي يستدعي parse_json_response(None)
                # فيفشل بـ AttributeError غير مُلتقَط يوقف خط الإنتاج بالكامل من
                # دون أي محاولة تدوير. الآن نعامله كخطأ مؤقت فيُعاد المحاولة
                # بنفس منطق 503/429 (نموذج/مفتاح تالٍ) بدل تسريب فشل صامت.
                finish_reason = getattr(response.candidates[0], "finish_reason", None)
                raise TransientError(
                    f"استجابة فارغة بلا نص فعلي من {model_name} (finish_reason={finish_reason})"
                )

            return response_text

        except Exception as e:  # noqa: BLE001 - نحتاج التقاط أي استثناء من SDK لتصنيفه
            last_error = e
            if _looks_like_rate_limit(e) or "api key not valid" in str(e).lower():
                logger.warning(
                    "[%s] فشل مصادقة/حصة على %s/%s — الانتقال للتالي. (%s)",
                    stage_name, rotator.current_key_name, model_name, e,
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


def upload_media_file(path: str) -> dict:
    """يعيد ملف الوسائط كجزء inline data جاهز للاستخدام المباشر في generate_content
    (Files API غير مستخدمة بعد الآن لأنها معطّلة في هذه الحزمة المتوقفة)."""
    return _load_inline_media_part(path)


def _extract_balanced_json_object(text: str) -> str:
    r"""يمسح النص ويوازن الأقواس المعقوفة { } (متجاهلاً ما بداخل السلاسل
    النصية والحروف المهرَّبة) لاستخراج أول كائن JSON متوازن فعليًا، بدل
    استخدام regex جشع (r"\{.*\}") يمسك من أول { إلى آخر } في كامل النص
    وقد يلتقط نصًا فاسدًا لو وُجد أي أقواس إضافية بعد الـ JSON الحقيقي."""
    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    # لم تتوازن الأقواس أبدًا (نص مقطوع) — أرجع بقية النص كما هو ليفشل
    # json.loads بوضوح بدل إخفاء الخطأ بصمت.
    return text[start:]


def _repair_common_json_issues(text: str) -> str:
    """يصلح أكثر أخطاء JSON شيوعًا الناتجة عن نماذج اللغة قبل التخلي والفشل:
    - فواصل زائدة قبل } أو ] (trailing commas).
    - أسطر جديدة حرفية (\n حقيقي) داخل قيم السلاسل النصية، والتي يجب أن تكون
      مهرَّبة (\\n) لكن النموذج أحيانًا يتركها كما هي فيكسر json.loads
      برسالة مثل "Expecting ',' delimiter" لأنها تبدو للـ parser كسطر جديد
      خارج السلسلة.
    هذه معالجة نصية بسيطة وليست بديلاً كاملاً عن parser JSON5، لكنها تغطي
    الغالبية العظمى من حالات الفشل الفعلية التي شوهدت في التشغيل."""
    # 1) فواصل زائدة: ,} أو ,]
    import re
    repaired = re.sub(r",(\s*[}\]])", r"\1", text)

    # 2) استبدال الأسطر الجديدة الحرفية داخل السلاسل النصية بـ \n مهرَّبة،
    # مع تتبع ما إذا كنا داخل سلسلة نصية أم لا (بنفس منطق موازنة الأقواس).
    out = []
    in_string = False
    escape = False
    for ch in repaired:
        if in_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def parse_json_response(raw_text: str) -> dict:
    """يستخرج أول كائن JSON متوازن {...} من النص عبر موازنة أقواس حقيقية
    (brace counting)، بدل الاعتماد فقط على إزالة أسوار Markdown أو على
    regex جشع، لأن النموذج قد يضيف جملة قبل/بعد الأقواس حتى مع
    response_mime_type='application/json'.

    عند فشل json.loads الأول (مثل "Expecting ',' delimiter" الذي أوقف
    مرحلة التدقيق النهائي بالكامل سابقًا رغم استجابة 200 OK صحيحة)، تُجرَّب
    محاولة ترميم للأخطاء الشائعة (فواصل زائدة، أسطر جديدة غير مهرَّبة داخل
    السلاسل) قبل رفع الاستثناء نهائيًا، بدل إسقاط خط الإنتاج بالكامل من أول
    خطأ تنسيق بسيط."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    cleaned = _extract_balanced_json_object(cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(
            "فشل json.loads الأول (%s) — محاولة ترميم أخطاء التنسيق الشائعة.", e
        )
        repaired = _repair_common_json_issues(cleaned)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # الترميم فشل أيضًا — نرفع الخطأ الأصلي بوضوح ليتعامل معه المستدعي
            # (بدل استثناء غامض على نص مختلف بعد الترميم)
            raise e
