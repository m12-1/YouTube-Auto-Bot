"""
script_writer.py
يكتب سكربت الفيديو الطويل (5 دقائق ~750-800 كلمة) وسكربت الشورت (~120 كلمة)
لنفس الموضوع، مع منطق تنظيف تلقائي لاستجابات JSON من Gemini.
"""
import json
import re
from scripts import config, gemini_client

LONG_SCRIPT_PROMPT = """
اكتب سكربت فيديو يوتيوب معلوماتي مشوّق باللغة الإنجليزية عن الموضوع التالي:
"{topic}"

شروط تقنية حاسمة للـ JSON:
1. ارجع JSON فقط (بدون أي مقدمات أو نصوص خارج الأقواس).
2. استخدم علامات اقتباس مزدوجة (") حصراً للهيكل.
3. إذا احتجت لاستخدام اقتباسات داخل النص، استبدلها بعلامة واحدة (').
4. تأكد أن الـ JSON صالح (Valid JSON).

الشروط المحتوائية:
- المدة المستهدفة: 5 دقائق (~750-800 كلمة)
- أول جملة hook صادم (لا مقدمات).
- بنية: hook -> 4 نقاط كقصة -> خاتمة + سؤال تفاعلي.
- ممنوع أي محتوى سياسي أو حساس أو مخالف للقيم الإسلامية.

أرجع JSON بهذا الشكل:
{{
  "title_draft": "...",
  "hook": "...",
  "scenes": [
    {{"scene_number": 1, "narration": "...", "visual_keywords": ["...", "..."]}},
    ...
  ],
  "closing_cta": "..."
}}
"""

SHORT_SCRIPT_PROMPT = """
حول هذا الموضوع لسكربت YouTube Short (أقل من 60 ثانية، ~120 كلمة)، بأسلوب صدمة.
الموضوع: "{topic}"

شروط تقنية حاسمة للـ JSON:
1. ارجع JSON فقط (بدون أي مقدمات).
2. استخدم علامات اقتباس مزدوجة (") للهيكل، واستبدل أي علامات داخل النص بعلامة (').
3. تأكد أن الـ JSON صالح (Valid JSON).

ممنوع منعاً باتاً أي محتوى مخالف للقيم الإسلامية أو إخباري حساس.

أرجع JSON بهذا الشكل:
{{"narration": "...", "visual_keywords": ["...", "..."]}}
"""

def _clean_json_response(raw: str) -> dict:
    """استخراج أول كائن JSON من النص وتجاهل أي نصوص إضافية."""
    try:
        # البحث عن النص المحصور بين أول '{' وآخر '}'
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            raw = match.group(0)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[ERROR] فشل تحليل JSON. النص المرجع: {raw[:300]}...")
        raise e

def write_long_script(topic: str) -> dict:
    prompt = LONG_SCRIPT_PROMPT.format(topic=topic)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    return _clean_json_response(raw)

def write_short_script(topic: str) -> dict:
    prompt = SHORT_SCRIPT_PROMPT.format(topic=topic)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    return _clean_json_response(raw)

def full_narration_text(long_script: dict) -> str:
    """يجمع كل السكربت كنص متصل لتمريره لـ TTS."""
    parts = [long_script["hook"]]
    parts += [s["narration"] for s in long_script["scenes"]]
    parts.append(long_script["closing_cta"])
    return " ".join(parts)
