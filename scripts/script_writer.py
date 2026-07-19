"""
script_writer.py
يكتب سكربت الفيديو الطويل (5 دقائق ~750-800 كلمة) وسكربت الشورت (~120 كلمة)
لنفس الموضوع، مع منطق تنظيف تلقائي لاستجابات JSON من Gemini.
"""
import json
import re
from scripts import config, gemini_client

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

Technical JSON Requirements:
1. Return ONLY the JSON object.
2. Use double quotes (") for the structure, replace inner quotes with (').
3. Valid JSON is mandatory.

Content Rules:
- No controversial/political/inappropriate content.
- STRICTLY FORBIDDEN: alcohol, drugs/narcotics, nudity, sexual content, gambling.
- Break the narration into 3-5 short scenes.
- Each scene: one short narration chunk + its OWN visual_keywords.

Return JSON in this format:
{{
  "hook": "first 1-2 sentences, the scroll-stopping opener",
  "scenes": [
    {{"scene_number": 1, "narration": "...", "visual_keywords": ["keyword1", "keyword2"]}},
    ...
  ],
  "closing_cta": "short call to action, e.g. follow for more"
}}
OUTPUT JSON ONLY:
"""

def _clean_json_response(raw: str) -> dict:
    """استخراج أول كائن JSON بدقة وتجاهل أي نصوص إضافية أو تعليقات."""
    try:
        # البحث عن أول { وآخر } لضمان استخراج الكائن كاملاً
        start = raw.find('{')
        end = raw.rfind('}')
        if start != -1 and end != -1:
            raw = raw[start:end+1]
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

def full_narration_text(script: dict) -> str:
    parts = [script["hook"]]
    parts += [s["narration"] for s in script["scenes"]]
    parts.append(script["closing_cta"])
    return " ".join(parts)
