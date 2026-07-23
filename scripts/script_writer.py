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

Visual Cut Rules (CRITICAL — this fixes a real bug where one static image stays on screen while the narration moves on to a different idea):
- Do NOT give one single set of keywords per scene. Break each scene's narration into 1 to 3 separate VISUAL CUTS — one for each distinct idea/beat mentioned in that scene, in the order they are spoken. A short single-idea scene can have just 1 cut; only split further if the narration clearly shifts to a different visual idea within the same scene.
- Each visual cut needs its own "keywords" (2-3 concrete filmable phrases, same rules as below) AND its own "duration_seconds": your best estimate of how long that specific idea takes to say out loud (assume ~2.3 spoken words per second). The exact number doesn't need to be perfect — the system rescales it automatically to match the real recorded audio — what matters is the RELATIVE proportion between cuts in the same scene.

Visual Keywords Rules (CRITICAL — these are literal stock-footage search queries, not abstract concepts):
- MANDATORY PHYSICAL-DESCRIPTION RULE: never output an abstract or philosophical noun phrase as a keyword. Convert every abstract idea into a literal physical object/scene a camera could film. Example: for the idea "a case study", do NOT write "case study" — write "old papers scattered on a desk in a dark room". For "innovation", write "glowing lightbulb on a wooden desk", not "innovation". For "trust", write "two people shaking hands close up", not "trust". If you catch yourself writing a keyword that names a concept/category/field of study rather than a physical scene, rewrite it before including it.
- Each visual_keywords entry must describe a CONCRETE, FILMABLE scene or object — something a camera could literally point at. Never use an abstract noun, adjective, or concept word on its own (e.g. NOT "digitally manipulated", "Japanese", "innovation", "mysterious").
- Write 2-4 word phrases combining a subject + visible action/setting, e.g. "hands typing laptop keyboard", "green falling code screen", "sushi platter close up", "person walking city street at night".
- Base each keyword on what is being narrated at THAT exact moment (i.e. that specific visual cut), not the general topic of the video. If the narration mentions a specific named thing (a film, dish, place, object), describe its literal visual appearance, not just its name — e.g. for "The Matrix code" write "green cascading code rain screen", not just "Matrix" or "code".
- NEVER produce a word-for-word / literal translation of the narration sentence into a keyword phrase. A keyword is not a translated snippet of the sentence — it is your own independent description of the VISUAL SCENE that idea implies. Re-express the meaning as something a camera could film; do not just carry over the sentence's wording or word order. A literal, word-by-word rendering almost never matches how real stock footage is tagged and will return irrelevant or empty results, ruining the scene.
- Avoid single abstract words entirely. If the concept has no direct visual (e.g. "manipulation", "concept", "idea"), pick the closest concrete real-world object or action that represents it visually instead (e.g. "hands editing photo on screen" instead of "digitally manipulated").
- Provide 2-3 keyword phrases per visual cut, ordered from most specific/accurate to more general, so a fallback still stays on-topic if the first is unavailable.
- Provide a `visual_intent` string describing the overall aesthetic and context of the scene (e.g. "cinematic hospital interior", "documentary style nature", "fast-paced technology").
- Provide a `negative_tags` list of 2-4 words describing things that MUST NOT appear because they would ruin the context (e.g. if the video is about medicine and the text says 'a new dawn', the negative tags should be ["nature", "sunrise", "landscape"] to prevent showing a literal sunrise).

Return JSON in this format:
{{
  "title_draft": "...",
  "hook": "...",
  "scenes": [
    {{"scene_number": 1, "narration": "...", "visuals": [
        {{"visual_intent": "...", "negative_tags": ["...", "..."], "keywords": ["concrete filmable phrase 1", "concrete filmable phrase 2"], "duration_seconds": 3.0}},
        {{"visual_intent": "...", "negative_tags": ["...", "..."], "keywords": ["concrete filmable phrase 1", "concrete filmable phrase 2"], "duration_seconds": 2.5}}
    ]}},
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

Visual Cut Rules (CRITICAL — this fixes a real bug where one static image stays on screen while the narration moves on to a different idea):
- Do NOT give one single set of keywords per scene. Break each scene's narration into 1 to 3 separate VISUAL CUTS — one for each distinct idea/beat mentioned in that scene, in the order they are spoken. A short single-idea scene can have just 1 cut; only split further if the narration clearly shifts to a different visual idea within the same scene.
- Each visual cut needs its own "keywords" (2-3 concrete filmable phrases, same rules as below) AND its own "duration_seconds": your best estimate of how long that specific idea takes to say out loud (assume ~2.3 spoken words per second, this is a fast-paced Short). The exact number doesn't need to be perfect — the system rescales it automatically to match the real recorded audio — what matters is the RELATIVE proportion between cuts in the same scene.

Visual Keywords Rules (CRITICAL — these are literal stock-footage search queries, not abstract concepts):
- MANDATORY PHYSICAL-DESCRIPTION RULE: never output an abstract or philosophical noun phrase as a keyword. Convert every abstract idea into a literal physical object/scene a camera could film. Example: for "a case study" write "old papers scattered on a desk in a dark room", not "case study". If you catch yourself writing a concept/category word instead of a physical scene, rewrite it.
- Each visual_keywords entry must describe a CONCRETE, FILMABLE scene or object — something a camera could literally point at. Never use an abstract noun, adjective, or concept word on its own (e.g. NOT "digitally manipulated", "Japanese", "innovation", "mysterious").
- Write 2-4 word phrases combining a subject + visible action/setting, e.g. "hands typing laptop keyboard", "green falling code screen", "sushi platter close up", "person walking city street at night".
- Base each keyword on what is being narrated at THAT exact moment (i.e. that specific visual cut), not the general topic of the video. If the narration mentions a specific named thing (a film, dish, place, object), describe its literal visual appearance, not just its name.
- NEVER produce a word-for-word / literal translation of the narration sentence into a keyword phrase. A keyword is not a translated snippet of the sentence — it is your own independent description of the VISUAL SCENE that idea implies. Re-express the meaning as something a camera could film; do not just carry over the sentence's wording or word order. A literal, word-by-word rendering almost never matches how real stock footage is tagged and will return irrelevant or empty results, ruining the scene.
- Avoid single abstract words entirely. If the concept has no direct visual, pick the closest concrete real-world object or action that represents it visually instead.
- Provide 2-3 keyword phrases per visual cut, ordered from most specific/accurate to more general, so a fallback still stays on-topic if the first is unavailable.
- Provide a `visual_intent` string describing the overall aesthetic and context of the scene.
- Provide a `negative_tags` list of 2-4 words describing things that MUST NOT appear because they would ruin the context.

Return JSON in this exact format (each narration value on ONE line only):
{{
  "hook": "first 1-2 sentences on a single line",
  "scenes": [
    {{"scene_number": 1, "narration": "single line narration here", "visuals": [
        {{"visual_intent": "...", "negative_tags": ["...", "..."], "keywords": ["concrete filmable phrase 1", "concrete filmable phrase 2"], "duration_seconds": 2.0}}
    ]}},
    {{"scene_number": 2, "narration": "single line narration here", "visuals": [
        {{"visual_intent": "...", "negative_tags": ["...", "..."], "keywords": ["concrete filmable phrase 1", "concrete filmable phrase 2"], "duration_seconds": 1.5}},
        {{"visual_intent": "...", "negative_tags": ["...", "..."], "keywords": ["concrete filmable phrase 1", "concrete filmable phrase 2"], "duration_seconds": 1.5}}
    ]}}
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


def _normalize_visuals(scene: dict) -> list[dict]:
    """
    توحّد شكل الوسائط البصرية لكل مشهد إلى قائمة visuals: [{"keywords": [...],
    "duration_seconds": float}, ...] بغض النظر عن الشكل اللي رجّعه Gemini:
    - الشكل الجديد المطلوب: scene["visuals"] موجودة فعلاً.
    - الشكل القديم (لو Gemini تجاهل التعليمات الجديدة): scene["visual_keywords"]
      فقط (قائمة كلمات مفتاحية مسطّحة) → تُغلّف كقطعة بصرية واحدة تغطي
      كامل مدة المشهد (نفس سلوك النسخة الأصلية بالضبط، بدون كسر شيء).
    """
    raw_visuals = scene.get("visuals")
    if isinstance(raw_visuals, list) and raw_visuals:
        cleaned = []
        for v in raw_visuals:
            if not isinstance(v, dict):
                continue
            keywords = v.get("keywords") or v.get("visual_keywords") or ["nature", "background"]
            if isinstance(keywords, str):
                keywords = [keywords]
            try:
                duration = float(v.get("duration_seconds", 2.0))
            except (TypeError, ValueError):
                duration = 2.0
            
            visual_intent = v.get("visual_intent", "")
            negative_tags = v.get("negative_tags", [])
            
            cleaned.append({
                "visual_intent": visual_intent,
                "negative_tags": negative_tags,
                "keywords": keywords, 
                "duration_seconds": max(0.3, duration)
            })
        if cleaned:
            return cleaned

    # الشكل القديم أو غياب الحقل بالكامل: نغلّف الكلمات المسطّحة كقطعة واحدة
    flat_keywords = scene.get("visual_keywords") or scene.get("keywords") or ["nature", "background"]
    if isinstance(flat_keywords, str):
        flat_keywords = [flat_keywords]
    word_count = max(1, len(scene.get("narration", "").split()))
    estimated_duration = round(word_count / 2.3, 1)  # ~2.3 كلمة/ثانية بالنطق الطبيعي
    
    visual_intent = scene.get("visual_intent", "")
    negative_tags = scene.get("negative_tags", [])
    
    return [{
        "visual_intent": visual_intent,
        "negative_tags": negative_tags,
        "keywords": flat_keywords, 
        "duration_seconds": estimated_duration
    }]


def _normalize_script(script: dict) -> dict:
    """
    يُوحِّد أسماء المفاتيح: Gemini أحياناً يُرجع 'cta' أو 'call_to_action'
    بدل 'closing_cta'، و'scenes' قد تكون فارغة أو مفقودة.
    يضمن أن كل المفاتيح المطلوبة موجودة بقيمة افتراضية آمنة، وأن كل مشهد
    عنده scene["visuals"] موحّدة الشكل (راجع _normalize_visuals) بغض النظر
    هل استخدم Gemini الشكل الجديد (قطع متعددة بمدد) أو القديم (كلمات مسطحة).
    """
    # توحيد closing_cta
    if "closing_cta" not in script:
        script["closing_cta"] = (
            script.pop("cta", None)
            or script.pop("call_to_action", None)
            or script.pop("outro", None)
            or "Follow for more amazing facts!"
        )

    # توحيد hook
    if "hook" not in script:
        script["hook"] = (
            script.pop("intro", None)
            or script.pop("opener", None)
            or ""
        )

    # ضمان وجود scenes كقائمة
    if "scenes" not in script or not isinstance(script.get("scenes"), list):
        script["scenes"] = []

    # ضمان أن كل مشهد عنده narration و visuals (بالشكل الموحّد الجديد)
    for scene in script["scenes"]:
        if "narration" not in scene:
            scene["narration"] = scene.get("text", scene.get("content", ""))
        scene["visuals"] = _normalize_visuals(scene)

    return script


def write_long_script(topic: str) -> dict:
    prompt = LONG_SCRIPT_PROMPT.format(topic=topic)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    return _normalize_script(_clean_json_response(raw))


def write_short_script(topic: str) -> dict:
    prompt = SHORT_SCRIPT_PROMPT.format(topic=topic)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    return _normalize_script(_clean_json_response(raw))

def full_narration_text(script: dict) -> str:
    parts = [script.get("hook", "")]
    parts += [s.get("narration", "") for s in script.get("scenes", [])]
    parts.append(script.get("closing_cta", ""))
    # تصفية الأجزاء الفارغة لتجنب مسافات مضاعفة
    return " ".join(p for p in parts if p.strip())


REPLACEMENT_KEYWORDS_PROMPT = """
A video narrator says exactly this line: "{narration}"

We searched stock footage/image libraries using these keyword phrases and found NOTHING usable (either no results at all, or every result was visually rejected as not matching the narration): {failed_keywords}

Your job: precisely re-target the search. Re-read the narration line above and isolate the ONE specific subject/action/object it is actually describing at this exact moment — not the general topic of the whole video. Then suggest 3 NEW alternative concrete, filmable stock-footage search phrases (2-4 words each) that:
1. Stay laser-focused on that exact subject/action (a camera operator should be able to picture the shot immediately from the phrase alone).
2. Are DIFFERENT from the failed phrases above — try a different visual angle, a different but equally accurate synonym, or a broader (still on-topic) version if the failed phrase was too niche/specific to exist as stock footage.
3. Are realistic as real stock-footage search terms: prefer common, filmable, real-world scenes/objects/actions over rare, abstract, or overly specific combinations that are unlikely to have matching footage.
4. Are NEVER a word-for-word / literal translation of the narration line — describe the VISUAL SCENE the idea implies in your own words, not a rephrasing that mirrors the sentence's wording or order. Literal renderings rarely match how real stock footage is tagged and will keep returning nothing, which is exactly the failure we are trying to fix.
5. Are ordered from the most precise/on-topic phrase to the safest still-relevant fallback, so a downstream fallback stays on-topic even if the first one still returns nothing.

Return ONLY a JSON array of exactly 3 strings, nothing else — no markdown, no explanation.
Example: ["phrase one here", "phrase two here", "phrase three here"]
"""


def suggest_replacement_visual(narration: str, failed_keywords: list[str]) -> list[str]:
    """
    تُستدعى من video_montage.py عندما تفشل كل محاولات جلب/تحقق وسائط قطعة
    بصرية معيّنة (3 مرشحين رُفضوا جميعاً أو تعذّر تحميلهم). نرجع لـ Gemini
    ونخبره بالكلمات التي فشلت، ونطلب بدائل جديدة لنفس جملة السرد، بدل
    الاستسلام مباشرة لصورة احتياطية عامة.

    تستخدم الآن الموديل المتقدم (key_type='advanced') بدل الخفيف — لأن
    دقة استهداف المشهد الصحيح هنا أهم من سرعة/تكلفة الاستدعاء (حسب الطلب:
    تحسين دقة البحث عن المشاهد عبر مفتاح Gemini المتقدم). لو استُنفدت حصة
    المفتاح المتقدم، generate_text تتراجع تلقائياً للموديل الخفيف بنفسها
    (راجع gemini_client.generate_text)، فلا داعٍ لأي منطق احتياطي إضافي هنا.
    ترجع قائمة فارغة (بدل رفع استثناء) لو فشل الاتصال بـ Gemini بالكامل —
    المستدعي (video_montage.py) يتعامل مع القائمة الفارغة بالانتقال مباشرة
    للاحتياط الأخير.
    """
    prompt = REPLACEMENT_KEYWORDS_PROMPT.format(
        narration=narration,
        failed_keywords=", ".join(f'"{k}"' for k in failed_keywords) or "(none)",
    )
    try:
        raw = gemini_client.generate_text(prompt, key_type="advanced", json_mode=False, temperature=0.6)
    except Exception as e:
        print(f"[SCRIPT WRITER WARNING] فشل طلب كلمات بديلة من Gemini: {e}")
        return []

    start, end = raw.find('['), raw.rfind(']')
    if start != -1 and end != -1:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, list):
                cleaned = [str(p).strip() for p in parsed if str(p).strip()]
                if cleaned:
                    return cleaned[:3]
        except json.JSONDecodeError:
            pass

    # احتياط أخير: لو ما كان الرد JSON صالحاً، نعتبر كل سطر غير فارغ عبارة
    lines = [l.strip(' -*"\'').strip() for l in raw.splitlines() if l.strip()]
    return lines[:3]
