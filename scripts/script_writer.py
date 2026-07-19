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
- Content must be family-friendly and free from controversial/political topics,
  and completely free of news events, real controversial public figures.
- STRICTLY FORBIDDEN, even indirectly: alcohol, drugs/narcotics, nudity or
  sexual content, gambling, or anything else considered inappropriate under
  Islamic values. Do not reference these topics in any way, even briefly.
- visual_keywords: 3-5 keywords per scene in ENGLISH ONLY.

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
"""

SHORT_SCRIPT_PROMPT = """
Convert the topic "{topic}" into a YouTube Short script (under 60s, ~120 words).
Style: Fast-paced, shocking hook in the first 2 seconds.

Technical JSON Requirements:
1. Return ONLY the JSON object.
2. Use double quotes (") for the structure, replace inner quotes with (').
3. Valid JSON is mandatory.

Content Rules:
- No controversial/political/inappropriate content, no news events or real
  controversial public figures.
- STRICTLY FORBIDDEN, even indirectly: alcohol, drugs/narcotics, nudity or
  sexual content, gambling, or anything else considered inappropriate under
  Islamic values. Do not reference these topics in any way, even briefly.
- Break the narration into 3-5 short scenes (a natural beat/idea change each
  time), NOT one giant block. This is required so visuals can change in sync
  with what is being said, instead of one static topic for the whole video.
- Each scene: one short narration chunk (1-3 sentences) + its OWN
  visual_keywords (2-4 keywords, ENGLISH ONLY) that describe what should be
  shown ON SCREEN during exactly that chunk, not the topic in general.

Return JSON in this format:
{{
  "hook": "first 1-2 sentences, the scroll-stopping opener",
  "scenes": [
    {{"scene_number": 1, "narration": "...", "visual_keywords": ["keyword1", "keyword2"]}},
    ...
  ],
  "closing_cta": "short call to action, e.g. follow for more"
}}
"""

def _clean_json_response(raw: str) -> dict:
    """استخراج أول كائن JSON من النص وتجاهل أي نصوص إضافية."""
    try:
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

def full_narration_text(script: dict) -> str:
    """
    يجمع كل السكربت كنص متصل لتمريره لـ TTS. تعمل الآن على شكل السكربت
    الطويل والشورت معاً لأن الاثنين صاروا بنفس البنية (hook/scenes/closing_cta)
    بعد تحديث SHORT_SCRIPT_PROMPT — هذا ضروري لمزامنة الوسائط مع كل مشهد
    (راجع voice_and_captions.map_scenes_to_timing).
    """
    parts = [script["hook"]]
    parts += [s["narration"] for s in script["scenes"]]
    parts.append(script["closing_cta"])
    return " ".join(parts)
