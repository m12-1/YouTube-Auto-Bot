"""
المرحلة 2: جمع الحقائق من Wikipedia باستخدام API مباشر.
يتجنب مكتبة wikipedia.org غير المستقرة ويستخدم requests مع User-Agent صحيح.
"""

import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; YouTubeAutoBot/1.0; +https://github.com/your-repo)"

def _fetch_wikipedia_summary(topic: str, lang: str = "en") -> Optional[str]:
    """
    يجلب ملخص الصفحة الأولى من ويكيبيديا بلغة معينة.
    يعيد None إذا لم يعثر على نتائج أو حدث خطأ.
    """
    url = f"https://{lang}.wikipedia.org/w/api.php"
    
    # 1. البحث عن الصفحة
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": topic,
        "srlimit": 1,
        "format": "json",
        "utf8": 1,
    }
    headers = {"User-Agent": USER_AGENT}
    
    try:
        resp = requests.get(url, params=search_params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("search", [])
        if not pages:
            return None
        
        page_id = pages[0]["pageid"]
        
        # 2. جلب النص (extract) لتلك الصفحة
        extract_params = {
            "action": "query",
            "pageids": page_id,
            "prop": "extracts",
            "exintro": True,       # فقط المقدمة
            "explaintext": True,   # نص عادي بدون HTML
            "format": "json",
            "utf8": 1,
        }
        
        extract_resp = requests.get(url, params=extract_params, headers=headers, timeout=15)
        extract_resp.raise_for_status()
        extract_data = extract_resp.json()
        
        pages_extract = extract_data.get("query", {}).get("pages", {})
        page = pages_extract.get(str(page_id), {})
        extract = page.get("extract", "").strip()
        
        # تنظيف بسيط: إزالة علامات المرجع [1] [2] إن وجدت
        import re
        clean_extract = re.sub(r'\[\d+\]', '', extract)
        return clean_extract
        
    except requests.exceptions.RequestException as e:
        logger.warning("فشل طلب Wikipedia (%s) عن '%s': %s", lang, topic, e)
        return None
    except Exception as e:
        logger.warning("خطأ غير متوقع في Wikipedia (%s) عن '%s': %s", lang, topic, e)
        return None

def collect_facts(topic: str) -> str:
    """
    الواجهة الرئيسية للمرحلة 2.
    تحاول الإنجليزية أولاً، ثم العربية، وتعيد النص أو سلسلة فارغة للتبديل إلى Gemini.
    """
    logger.info("جمع الحقائق من Wikipedia (EN) عن: %s", topic)
    facts_en = _fetch_wikipedia_summary(topic, "en")
    if facts_en:
        logger.info("تم جلب %d حرف من Wikipedia الإنجليزية.", len(facts_en))
        return facts_en

    logger.info("لم يتم العثور على نتائج إنجليزية، المحاولة بالعربية...")
    facts_ar = _fetch_wikipedia_summary(topic, "ar")
    if facts_ar:
        logger.info("تم جلب %d حرف من Wikipedia العربية.", len(facts_ar))
        return facts_ar

    logger.warning("لا نتائج Wikipedia في أي لغة. سيتم توليد الحقائق عبر Gemini (كخطة احتياطية).")
    return ""  # القيمة الفارغة ستؤدي إلى توليد Gemini داخل script_and_seo_planner
