"""
script_writer.py
يكتب سكربت الفيديو الطويل (5 دقائق ~750-800 كلمة) وسكربت الشورت (~120 كلمة)
لنفس الموضوع، مع منطق تنظيف تلقائي لاستجابات JSON من Gemini.
"""
import json
import re
from scripts import config, gemini_client


def _sanitize_json_string(raw: str) -> str:
    """
    يُصلح JSON الذي يحتوي على سطور جديدة حرفية (literal newlines) داخل
    قيم النصوص — وهو الخطأ الشائع من Gemini عند إرجاع نصوص طويلة.
    يعمل حرفاً بحرف لضمان عدم المساس ببنية JSON الخارجية.
    """
    result = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif ch == '\n' and in_string:
            result.append('\\n')  # فاصل سطر داخل نص → يصبح \n مشفّر
        elif ch == '\r' and in_string:
            result.append('\\r')
        elif ch == '\t' and in_string:
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)

LONG_SCRIPT_PROMPT = """
Write an engaging YouTube information video script in English about: "{topic}"

Technical JSON Requirements:
1. Return ONLY the JSON object. No preambles.
2. Use double quotes (") for the structure.
3. If quotes are needed inside text, use single quotes (').
4. Valid JSON is mandatory.

Content Rules:
- Duration: 5 mins (~750-800 words).
- Start with a shocking hook.
- Structure: hook -> 4 story points -> impact conclusion + call to action.
- Content must be family-friendly and free from controversial/political topics.
- STRICTLY FORBIDDEN: alcohol, drugs/narcotics, nudity, sexual content, gambling.

Return JSON in this format:
{{
  "title_draft": "...",
  "hook": "...",
  "scenes": [
    {{"scene_number": 1, "narration": "...", "visual_keywords": ["keyword1", "keyword2"]}},
    ...
  ],
  "closing_cta": "..."
}}
OUTPUT JSON ONLY:
"""

SHORT_SCRIPT_PROMPT = """
Convert the topic "{topic}" into a YouTube Short script (under 60s, ~120 words).
Style: Fast-paced, shocking hook in the first 2 seconds.

CRITICAL JSON Rules (violations will break the system):
1. Return ONLY the JSON object — no markdown, no preamble, no explanation.
2. ALL string values MUST be on a SINGLE LINE — absolutely NO literal newlines inside strings.
3. Use double quotes (") for all keys and values.
4. Replace ANY double quote inside text with a single quote (').
5. Replace ANY newline or line break inside text with a space instead.
6. No trailing commas after the last item in arrays or objects.

Content Rules:
- No controversial/political/inappropriate content.
- STRICTLY FORBIDDEN: alcohol, drugs/narcotics, nudity, sexual content, gambling.
- Break the narration into 3-5 short scenes (each narration MUST be one single line).
- Each scene: one short narration chunk + its OWN visual_keywords.

Return JSON in this exact format (each value on ONE line only):
{{
  "hook": "first 1-2 sentences on a single line",
  "scenes": [
    {{"scene_number": 1, "narration": "single line narration here", "visual_keywords": ["keyword1", "keyword2"]}},
    {{"scene_number": 2, "narration": "single line narration here", "visual_keywords": ["keyword1", "keyword2"]}}
  ],
  "closing_cta": "short call to action on a single line"
}}
OUTPUT JSON ONLY:
"""

def _clean_json_response(raw: str) -> dict:
    """
    استخراج JSON من استجابة Gemini مع معالجة الأخطاء الشائعة:
    - نصوص إضافية قبل/بعد الكائن
    - سطور جديدة حرفية داخل قيم النصوص (الخطأ الأكثر شيوعاً)
    - trailing commas
    """
    # الخطوة 1: استخراج أول كائن JSON فقط
    start = raw.find('{')
    end = raw.rfind('}')
    if start != -1 and end != -1:
        raw = raw[start:end+1]

    # الخطوة 2: محاولة التحليل المباشر
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # الخطوة 3: تنظيف السطور الجديدة داخل النصوص (السبب الأكثر شيوعاً للفشل)
    try:
        sanitized = _sanitize_json_string(raw)
        return json.loads(sanitized)
    except json.JSONDecodeError:
        pass

    # الخطوة 4: إزالة trailing commas ثم إعادة المحاولة
    try:
        no_trailing = re.sub(r',\s*([}\]])', r'\1', raw)
        sanitized2 = _sanitize_json_string(no_trailing)
        return json.loads(sanitized2)
    except json.JSONDecodeError:
        pass

    # الخطوة 5 (الملاذ الأخير): استخدام مكتبة json-repair المتخصصة
    try:
        from json_repair import repair_json
        repaired = repair_json(raw, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
    except Exception:
        pass

    print(f"[ERROR] فشل تحليل JSON بعد كل محاولات التنظيف. النص المرجع:\n{raw[:500]}")
    raise json.JSONDecodeError("All JSON repair attempts failed", raw, 0)

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

def full_narration_text(script: dict) -> str:
    parts = [script["hook"]]
    parts += [s["narration"] for s in script["scenes"]]
    parts.append(script["closing_cta"])
    return " ".join(parts)
