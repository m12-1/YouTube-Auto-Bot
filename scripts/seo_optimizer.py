"""
seo_optimizer.py
هذا أهم ملف بالمشروع حسب طلب المستخدم صراحة: "السيو أقل ما يقال عنه عظيم".
يجمع: تحليل أنماط أفضل 10 منافسين + الكلمات المفتاحية للموضوع + قواعد SEO
الصارمة ليوتيوب، ويبني عنوان/وصف/وسوم/فصول محسّنة بالكامل — بدون نسخ حرفي
من المنافسين (تحليل نمط فقط، لتفادي "Inauthentic Content").
"""
import json
from scripts import config, gemini_client, competitor_seo

SEO_PROMPT = """
أنت خبير YouTube SEO محترف بجمهور أمريكي. مهمتك بناء بيانات وصفية (metadata)
بأعلى معايير الاحترافية لفيديو معلوماتي، بناءً على تحليل أنماط ناجحة فعلياً
(وليس نسخها حرفياً — ممنوع منعاً باتاً استخدام أي عنوان أو جملة موجودة حرفياً
بالمنافسين، فقط استخلص الأنماط والكلمات المفتاحية الفعّالة).

الموضوع الأساسي: "{topic}"

أفضل 10 فيديوهات منافسة حالياً على نفس الموضوع (عنوان | مشاهدات):
{competitor_summary}

قواعد صارمة يجب اتباعها بالكامل:
1. العنوان (title): 60-70 حرف كحد أقصى (يقص يوتيوب أكثر من هذا)، يحتوي الكلمة
   المفتاحية الرئيسية بأول 5 كلمات، أسلوب فضول قوي بدون مبالغة كاذبة (no clickbait lies)
2. الوصف (description):
   - أول 2-3 أسطر (150 حرف) هي الأهم لأنها تظهر قبل "show more" — يجب أن تحتوي
     الكلمة المفتاحية الرئيسية وتلخص القيمة بوضوح
   - فقرة موسّعة (150-300 كلمة) تشرح محتوى الفيديو بأسلوب طبيعي مع تكرار طبيعي
     للكلمات المفتاحية الثانوية (بدون keyword stuffing مبالغ فيه)
   - قائمة Chapters بصيغة (00:00 Intro) لكل مشهد رئيسي بالسكربت
   - سطر يدعو للاشتراك + 3-5 هاشتاقات ذات صلة بنهاية الوصف
3. الوسوم (tags): 15-20 وسم، من عام لخاص (broad -> specific)، بدون تكرار،
   بدون وسوم مضللة غير متعلقة فعلياً بالمحتوى
4. الكل باللغة الإنجليزية، موجه لجمهور أمريكي

السكربت (للفصول والسياق):
{script_json}

أرجع JSON فقط بهذا الشكل بالضبط:
{{
  "title": "...",
  "description": "...",
  "tags": ["...", "..."],
  "chapters": [{{"timestamp": "00:00", "label": "..."}}, ...],
  "hashtags": ["#...", "#..."]
}}
"""


def _summarize_competitors(competitors: list[dict]) -> str:
    lines = [f"- {c['title']} | {c['views']:,} views" for c in competitors]
    return "\n".join(lines) if lines else "لا توجد بيانات منافسين متاحة اليوم."


def build_seo_metadata(topic: str, long_script: dict) -> dict:
    competitors = competitor_seo.get_top_competitors(topic)
    prompt = SEO_PROMPT.format(
        topic=topic,
        competitor_summary=_summarize_competitors(competitors),
        script_json=json.dumps(long_script, ensure_ascii=False),
    )
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SEO, key_type="advanced",
        json_mode=True, temperature=0.6,
    )
    metadata = json.loads(raw)

    # حماية إضافية: قص العنوان إذا تجاوز الحد رغم التعليمات
    if len(metadata.get("title", "")) > 70:
        metadata["title"] = metadata["title"][:67] + "..."

    return metadata
