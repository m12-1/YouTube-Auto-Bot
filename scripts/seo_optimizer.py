"""
seo_optimizer.py
هذا أهم ملف بالمشروع حسب طلب المستخدم صراحةً: "السيو أقل ما يقال عنه عظيم".
يجمع: تحليل أنماط أفضل 10 منافسين + الكلمات المفتاحية للموضوع + قواعد SEO
الصارمة ليوتيوب، ويبني عنوان/وصف/وسوم/فصول محسّنة بالكامل — بدون نسخ حرفي
من المنافسين (تحليل نمط فقط، لتفادي "إنتج أصلي " غير أصيل).
"""
import json
import re
from scripts import config, gemini_client, competitor_seo


def _sanitize_json_string(raw: str) -> str:
    """نفس منطق script_writer: يُصلح السطور الجديدة الحرفية داخل قيم النصوص."""
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
            result.append('\\n')
        elif ch == '\r' and in_string:
            result.append('\\r')
        elif ch == '\t' and in_string:
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


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

شروط تقنية حاسمة للـ JSON:
1. أرجع JSON فقط بدون أي نصوص أو مقدمات.
2. استخدم علامات اقتباس مزدوجة (") حصراً للمفاتيح والقيم.
3. استبدل أي علامة اقتباس مزدوجة داخل النصوص بعلامة مفردة (').
4. تأكد من عدم وجود فواصل (,) زائدة في نهاية القوائم أو الكائنات (No trailing commas).
5. تأكد أن الـ JSON صالح وقابل للقراءة (Valid JSON).

السكربت (للفصول والسياق):
{script_json}

أرجع JSON فقط بهذا الشكل بالضبط:
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
    return "\n".join(lines) if lines else "لا توجد بيانات منافسين متاحة اليوم."


def _clean_json_response(raw: str) -> dict:
    """استخراج كائن JSON من النص مع معالجة الأخطاء الشائعة من Gemini."""
    start = raw.find('{')
    end = raw.rfind('}')
    if start != -1 and end != -1:
        raw = raw[start:end+1]

    # محاولة 1: تحليل مباشر
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # محاولة 2: تنظيف السطور الجديدة داخل النصوص
    try:
        return json.loads(_sanitize_json_string(raw))
    except json.JSONDecodeError:
        pass

    # محاولة 3: إزالة trailing commas + تنظيف
    try:
        cleaned = re.sub(r',\s*([}\]])', r'\1', raw)
        return json.loads(_sanitize_json_string(cleaned))
    except json.JSONDecodeError as e:
        print(f"[SEO ERROR] فشل تحليل JSON. سيتم استخدام SEO أساسي لضمان النشر. الخطأ: {e}")
        return None


def build_seo_metadata(topic: str, long_script: dict) -> dict:
    # جلب المنافسين بشكل آمن — فشل YouTube API لا يوقف باقي عملية السيو
    try:
        competitors = competitor_seo.get_top_competitors(topic)
    except Exception as e:
        print(f"[SEO WARNING] فشل جلب بيانات المنافسين: {e}. سيعمل Gemini بدون بيانات منافسين.")
        competitors = []
    prompt = SEO_PROMPT.format(
        topic=topic,
        competitor_summary=_summarize_competitors(competitors),
        script_json=json.dumps(long_script, ensure_ascii=False),
    )

    metadata = None
    # محاولتان بدرجة حرارة منخفضة تدريجياً قبل اللجوء للسيو الاحتياطي الضعيف —
    # لأن السيو هو أهم جزء بالمشروع حسب طلبك، لا نستسلم من أول فشل بارسنق
    for attempt, temperature in enumerate([0.6, 0.3]):
        raw = gemini_client.generate_text(
            prompt, model=config.MODEL_SEO, key_type="advanced",
            json_mode=True, temperature=temperature,
        )
        metadata = _clean_json_response(raw)
        if metadata:
            break
        print(f"[SEO] محاولة {attempt + 1} فشلت بتحليل JSON، إعادة المحاولة...")

    # نظام الإنقاذ الأخير: فقط لو فشلت كل المحاولات فعلياً
    if not metadata:
        safe_topic = topic.replace('"', '').replace("'", "")
        metadata = {
            "title": f"The Hidden Truth About {safe_topic[:40]}",
            "description": f"Discover amazing facts and the hidden truth about {safe_topic}.\n\nSubscribe for more daily videos!\n\n#shorts #facts",
            "tags": ["facts", "interesting", "knowledge", safe_topic],
            "chapters": [{"timestamp": "00:00", "label": "Intro"}],
            "hashtags": ["#shorts", "#facts", "#viral"]
        }

    # حماية إضافية: قص العنوان إذا تجاوز الحد رغم التعليمات
    if len(metadata.get("title", "")) > 70:
        metadata["title"] = metadata["title"][:67] + "..."

    return metadata
