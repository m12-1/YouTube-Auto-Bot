"""
asset_fetcher.py
يعتمد على Pixabay حصرياً حالياً (Pexels معطّل بسبب مشكلة مفتاح سابقة، يمكن
إعادة تفعيله لاحقاً بـ fetch_pexels أدناه إذا صار المفتاح صالحاً).

إصلاحات هذه النسخة:
- إزالة رابط placeholder وهمي غير حقيقي كان يسبب فشل تحميل صامت
- دعم orientation ديناميكي (عمودي للشورت، أفقي للطويل) بدل "horizontal" ثابت
- get_images_for_scene يرجع فقط روابط، والتحقق من نجاح التحميل الفعلي بـ download_image

إضافة جديدة (مزيج فيديو + صور):
- fetch_pixabay_videos(): نفس مفتاح Pixabay الحالي، بدون أي سر إضافي مطلوب
  بـ GitHub Secrets، يجلب مقاطع فيديو حرة الحقوق (footage) بدل صور ثابتة فقط.
- get_media_for_scene(): تعطي كل مشهد "وسيط" (video أو image) بدل صورة فقط،
  مع fallback تلقائي لصورة لو ما لقى فيديو مناسب للكلمة المفتاحية.

إصلاح مشكلة "الفيديوهات بعيدة عن الموضوع":
- _relevance_score(): تقارن كلمات الكلمة المفتاحية مع حقل tags من Pixabay.
- get_media_for_scene تجرب كل الكلمات المفتاحية وتختار الأفضل تطابقاً.
- topic_context: يُرفق موضوع الفيديو مع كل بحث لمنع الانحراف عن السياق.
- إزالة فلتر الاتجاه (vertical/horizontal) لأن 95%+ من فيديوهات Pixabay
  أفقية — Remotion يقص ويملأ تلقائياً عبر objectFit:'cover'.
"""
import re
import random

import requests
from requests.utils import quote
from scripts import config

MIN_WIDTH = 1080  # مخفّض من 1920 لأن أغلب صور Pixabay العمودية أضيق من هذا

# نسبة تفضيل الفيديو مقابل الصورة لكل مشهد (0.70 = يحاول فيديو أولاً بـ 70%
# من المشاهد و30% صور، حسب الطلب: فيديوهات 70% / صور 30%)
VIDEO_PREFERENCE_RATIO = 0.70

# أقل درجة تطابق (0-1) نقبلها بين الكلمة المفتاحية ووسوم Pixabay قبل اعتبار
# النتيجة "غير ذات صلة كافية" والانتقال للكلمة المفتاحية التالية بالمشهد
MIN_RELEVANCE_SCORE = 0.2


def _re_split_words(text: str) -> list[str]:
    """تقسيم بسيط لكلمات نص (يتجاهل الفواصل وعلامات الترقيم)."""
    return [w for w in re.split(r'[^a-zA-Z0-9]+', text) if w]


def _relevance_score(keyword: str, tags: str) -> float:
    """
    تقيس نسبة تداخل كلمات الكلمة المفتاحية مع وسوم Pixabay الراجعة (tags
    نص مفصول بفواصل مثل "matrix, code, green, technology"). ترجع نسبة من
    0 إلى 1: عدد كلمات الكلمة المفتاحية الموجودة فعلياً ضمن الوسوم مقسومة
    على عدد كلمات الكلمة المفتاحية الكلي.

    المطابقة تشمل تطابق البادئة (startswith) بطول 4 أحرف فأكثر، لالتقاط
    اختلافات الجذر اللغوي الشائعة (Japanese/Japan, walking/walk).
    """
    kw_words = [w.lower() for w in _re_split_words(keyword)]
    if not kw_words:
        return 0.0
    tag_words = [w.lower() for w in _re_split_words(tags)]
    if not tag_words:
        return 0.0

    def word_matches(kw_word: str) -> bool:
        for tw in tag_words:
            if kw_word == tw:
                return True
            if len(kw_word) >= 4 and len(tw) >= 4 and (kw_word.startswith(tw) or tw.startswith(kw_word)):
                return True
        return False

    matched = sum(1 for w in kw_words if word_matches(w))
    return matched / len(kw_words)


def fetch_pixabay(keyword: str, per_page: int = 3, orientation: str = "horizontal") -> list[str]:
    if not config.PIXABAY_API_KEY:
        return []

    # إصلاح الخطأ 400: واجهة Pixabay ترفض أي قيمة أقل من 3
    api_per_page = max(3, per_page)

    encoded_keyword = quote(keyword)
    url = (
        f"https://pixabay.com/api/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}"
        f"&image_type=photo&orientation={orientation}&per_page={api_per_page}&safesearch=true"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        return [h["largeImageURL"] for h in hits]
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pixabay لـ '{keyword}' (orientation={orientation}): {e}")
        return []


def fetch_pexels(keyword: str, per_page: int = 3, orientation: str = "landscape") -> list[str]:
    if not config.PEXELS_API_KEY:
        return []
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": keyword, "per_page": per_page, "orientation": orientation}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        return [p["src"]["large2x"] for p in photos]
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pexels لـ '{keyword}': {e}")
        return []


def get_images_for_scene(keywords: list[str], target_count: int = 4,
                          is_short: bool = True) -> list[str]:
    """
    is_short=True يطلب صوراً عمودية (تناسب 1080x1920 بدون قص كبير)، وإلا يطلب
    أفقية. لو الكلمة المحددة ما رجعت نتائج، يجرب كلمات احتياطية عامة بدل ما
    يرجع placeholder وهمي غير قابل للتحميل (كان هذا الخطأ بالنسخة السابقة).
    """
    orientation = "vertical" if is_short else "horizontal"
    images = []
    for kw in keywords:
        images += fetch_pixabay(kw, per_page=target_count, orientation=orientation)
        if len(images) >= target_count:
            break

    if not images:
        for fallback_kw in ["abstract background", "nature", "sky"]:
            images += fetch_pixabay(fallback_kw, per_page=target_count, orientation=orientation)
            if images:
                break

    return images[:target_count]


def fetch_pixabay_videos(keyword: str, per_page: int = 3) -> list[dict]:
    """
    يستخدم نفس PIXABAY_API_KEY الموجود أصلاً (لا يحتاج سر جديد بـ Secrets).
    يرجع قائمة dicts فيها {"url", "width", "height", "score"} مرتّبة من
    الأعلى تطابقاً مع الكلمة المفتاحية للأقل.

    ملاحظة مهمة: لا نفلتر حسب الاتجاه (عمودي/أفقي) لأن:
    - 95%+ من فيديوهات Pixabay أفقية
    - Remotion يتعامل مع القص تلقائياً عبر objectFit:'cover'
    - حذف الفلتر يعني نتائج فيديو أكثر بكثير بدل السقوط للصور الثابتة
    """
    if not config.PIXABAY_API_KEY:
        return []

    # نطلب أكثر من المطلوب فعلياً (حد أقصى 10) لإعطاء خوارزمية التطابق
    # مرشحين كفاية للاختيار بينهم بدل الاكتفاء بأول 3 نتائج فقط
    api_per_page = max(3, min(10, per_page * 4))

    encoded_keyword = quote(keyword)
    url = (
        f"https://pixabay.com/api/videos/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}"
        f"&video_type=film&per_page={api_per_page}&safesearch=true"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        results = []
        for h in hits:
            videos = h.get("videos", {})
            chosen = videos.get("medium") or videos.get("small") or videos.get("large")
            if not chosen or not chosen.get("url"):
                continue
            width, height = chosen.get("width", 0), chosen.get("height", 0)
            score = _relevance_score(keyword, h.get("tags", ""))
            results.append({"url": chosen["url"], "width": width, "height": height, "score": score})
        # الأعلى تطابقاً أولاً
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pixabay Video لـ '{keyword}': {e}")
        return []


def get_media_for_scene(keywords: list[str], target_count: int = 1,
                         is_short: bool = True, prefer_video: bool = True,
                         topic_context: str = "") -> list[dict]:
    """
    ترجع قائمة عناصر media بالشكل {"type": "video"|"image", "url": "..."}.

    topic_context: الموضوع الرئيسي للفيديو — يُضاف لكل كلمة مفتاحية لضمان
    بقاء النتائج ضمن السياق الصحيح (مثلاً: "survival" → "survival gaming"
    بدل مقاطع طبيعة عن البقاء في البرية).

    المنطق: تجرب كل الكلمات المفتاحية للمشهد وتحتفظ بأفضل نتيجة حسب
    درجة التطابق مع وسوم Pixabay. لو أفضل نتيجة أقل من MIN_RELEVANCE_SCORE
    تتجاهلها وتنتقل لكلمات fallback عامة.
    """
    media: list[dict] = []

    # إرفاق الموضوع الرئيسي مع كل كلمة مفتاحية لمنع الانحراف عن السياق
    contextualized_keywords = []
    for kw in keywords:
        if topic_context:
            contextualized_keywords.append(f"{kw} {topic_context}".strip())
        contextualized_keywords.append(kw)  # نضيف الكلمة وحدها أيضاً كبديل

    if prefer_video:
        best_score = -1.0
        best_vids = None
        for kw in contextualized_keywords:
            vids = fetch_pixabay_videos(kw, per_page=target_count)
            if not vids:
                continue
            top_score = vids[0]["score"]
            if top_score > best_score:
                best_score = top_score
                best_vids = vids

        # لو أفضل تطابق وجدناه أقل من الحد الأدنى المقبول، نعتبره غير كافٍ
        if best_vids and best_score >= MIN_RELEVANCE_SCORE:
            media = [{"type": "video", "url": v["url"]} for v in best_vids[:target_count]]
        elif best_vids and best_score < MIN_RELEVANCE_SCORE:
            print(f"[ASSET WARNING] أفضل تطابق فيديو لكلمات {keywords} كانت درجته {best_score:.2f} "
                  f"(أقل من الحد الأدنى {MIN_RELEVANCE_SCORE})، سيتم تجربة كلمات fallback عامة.")

        # لو فشلت كل الكلمات، جرب كلمات عامة للفيديو قبل السقوط للصور
        if not media:
            for fallback_vid_kw in ["cinematic", "aerial", "timelapse", "urban", "nature footage"]:
                vids = fetch_pixabay_videos(fallback_vid_kw, per_page=target_count)
                if vids:
                    media = [{"type": "video", "url": v["url"]} for v in vids[:target_count]]
                    break

    if not media:
        images = get_images_for_scene(keywords, target_count=target_count, is_short=is_short)
        media = [{"type": "image", "url": u} for u in images]

    if not media:
        for fallback_kw in ["abstract background", "nature", "sky"]:
            images = get_images_for_scene([fallback_kw], target_count=target_count, is_short=is_short)
            if images:
                media = [{"type": "image", "url": u} for u in images]
                break

    return media[:target_count]


def download_video(url: str, out_path: str):
    """نفس منطق download_image لكن للفيديو — يرجع None لو فشل التحميل فعلياً
    بدل تمرير رابط ميت للرندرة."""
    try:
        r = requests.get(url, timeout=40, stream=True)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        return out_path
    except Exception as e:
        print(f"[ASSET ERROR] فشل تحميل الفيديو من {url}: {e}")
        return None


def download_image(url: str, out_path: str):
    """يرجع المسار لو نجح التحميل فعلياً، أو None لو فشل."""
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return out_path
    except Exception as e:
        print(f"[ASSET ERROR] فشل تحميل الصورة من {url}: {e}")
        return None

def verify_media_file(file_path: str, narration: str) -> bool:
    """
    يقوم بالتحقق البصري من ملف الوسائط (فيديو أو صورة) باستخدام Gemini Vision.
    إذا كان فيديو، يستخرج إطاراً من الثانية 0.5 للتحقق.
    """
    from scripts import gemini_client
    import subprocess
    import os
    
    check_path = file_path
    is_temp = False
    
    # إذا كان فيديو، استخرج صورة مصغرة
    if file_path.lower().endswith(('.mp4', '.webm', '.mov')):
        thumb_path = file_path + "_thumb.jpg"
        try:
            # نستخرج إطار من الثانية 0.5 (أو أول إطار متاح) بجودة منخفضة لتسريع الفحص
            cmd = [
                "ffmpeg", "-y", "-i", file_path, 
                "-ss", "00:00:00.500", "-vframes", "1", 
                "-q:v", "5", thumb_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            check_path = thumb_path
            is_temp = True
        except Exception as e:
            print(f"[ASSET WARNING] فشل استخراج صورة من الفيديو للتحقق ({e})، سيتم افتراض نجاح التحقق.")
            return True
            
    # التحقق عبر Gemini
    try:
        is_relevant = gemini_client.verify_media_relevance(check_path, narration)
        if not is_relevant:
            print(f"[ASSET FILTER] تم رفض الوسائط. Gemini قرر أنها لا تطابق: {narration[:50]}...")
        return is_relevant
    except Exception as e:
        print(f"[ASSET WARNING] فشل الاتصال بـ Gemini للتحقق ({e})، سيتم افتراض نجاح التحقق لتجنب توقف الإنتاج.")
        return True
    finally:
        if is_temp and os.path.exists(check_path):
            try:
                os.remove(check_path)
            except:
                pass

