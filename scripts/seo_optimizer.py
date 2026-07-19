"""
seo_optimizer.py
تحليل SEO احترافي مع معالجة أخطاء YouTube API.
"""
import json
import re
from scripts import config, gemini_client, competitor_seo

SEO_PROMPT = """
أنت خبير YouTube SEO محترف بجمهور أمريكي. مهمتك بناء بيانات وصفية (metadata)
بأعلى معايير الاحترافية لفيديو معلوماتي.

الموضوع الأساسي: "{topic}"
أفضل 10 فيديوهات منافسة حالياً:
{competitor_summary}

القواعد: العنوان (60-70 حرف)، الوصف (ملخص في أول 150 حرف، فقرة شرح)، 
قائمة Chapters، هاشتاقات.

شروط تقنية حاسمة للـ JSON:
1. أرجع JSON فقط.
2. استبدل " بـ '.
3. لا فواصل زائدة.

السكربت: {script_json}

أرجع JSON بهذا الشكل:
{{
  "title": "...",
  "description": "...",
  "tags": ["...", "..."],
  "chapters": [{{"timestamp": "00:00", "label": "..."}}],
  "hashtags": ["#...", "#..."]
}}
"""

def _summarize_competitors(competitors: list[dict]) -> str:
    lines = [f"- {c['title']} | {c['views']:,} views" for c in competitors]
    return "\n".join(lines) if lines else "لا توجد بيانات منافسين."

def _clean_json_response(raw: str) -> dict:
    try:
        start = raw.find('{')
        end = raw.rfind('}')
        if start != -1 and end != -1:
            raw = raw[start:end+1]
        return json.loads(raw)
    except:
        return None

def build_seo_metadata(topic: str, long_script: dict) -> dict:
    try:
        # تغليف الاستدعاء بـ try/except لمنع انهيار البرنامج
        competitors = competitor_seo.get_top_competitors(topic)
    except Exception as e:
        print(f"[SEO WARNING] فشل جلب المنافسين: {e}. سيُستخدم Gemini بدونهم.")
        competitors = []

    prompt = SEO_PROMPT.format(
        topic=topic,
        competitor_summary=_summarize_competitors(competitors),
        script_json=json.dumps(long_script, ensure_ascii=False),
    )

    metadata = None
    for attempt, temperature in enumerate([0.6, 0.3]):
        raw = gemini_client.generate_text(
            prompt, model=config.MODEL_SEO, key_type="advanced",
            json_mode=True, temperature=temperature,
        )
        metadata = _clean_json_response(raw)
        if metadata:
            break

    if not metadata:
        metadata = {
            "title": f"The Hidden Truth About {topic[:40]}",
            "description": "Amazing facts! #shorts #facts",
            "tags": ["facts", topic],
            "chapters": [{"timestamp": "00:00", "label": "Intro"}],
            "hashtags": ["#shorts", "#facts"]
        }
    return metadata
