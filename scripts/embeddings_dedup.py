"""
embeddings_dedup.py
يمنع تكرار نفس الموضوع خلال آخر 60 يوم عبر مقارنة embeddings (cosine similarity)
بدل المطابقة النصية البسيطة — أدق بكثير ويحمي من "Inauthentic/Reused Content".
مُحسّن لحفظ الـ Embeddings في الذاكرة لتجنب خطأ Quota exceeded (429).
"""
import numpy as np
from datetime import datetime, timedelta

from scripts import config, sheets_client, gemini_client

SIMILARITY_THRESHOLD = 0.87  # فوق هذا الرقم يعتبر نفس الموضوع تقريباً

# ذاكرة مؤقتة (Cache) لتخزين العناوين القديمة مع الـ Embeddings الخاصة بها لمنع إعادة استدعاء API
_OLD_TITLES_EMBEDDINGS_CACHE = {}
_CACHED_SPREADSHEET_ID = None
_LAST_FETCH_TIME = None
_RECENT_TITLES_LIST = []


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def _load_recent_titles_once(spreadsheet_id: str):
    """يقرأ السجلات من جوجل شيت مرة واحدة فقط ويحفظ العناوين النشطة بالذاكرة لتوفير الحصة."""
    global _RECENT_TITLES_LIST, _CACHED_SPREADSHEET_ID, _LAST_FETCH_TIME
    
    now = datetime.utcnow()
    # إذا كانت البيانات محملة مسبقاً ولم يمر عليها أكثر من 5 دقائق، لا داعي لإعادة القراءة من جوجل شيت
    if _CACHED_SPREADSHEET_ID == spreadsheet_id and _LAST_FETCH_TIME and (now - _LAST_FETCH_TIME) < timedelta(minutes=5):
        return

    try:
        records = sheets_client.get_all_records(spreadsheet_id, config.Paths().sheets_trend_log)
    except Exception as e:
        print(f"[DEDUP WARNING] فشل قراءة السجل من Sheets: {e}")
        records = []

    cutoff = now - timedelta(days=config.DEDUP_LOOKBACK_DAYS)
    recent_titles = []
    
    for r in records:
        try:
            row_date = datetime.fromisoformat(str(r.get("date", "")))
            if row_date >= cutoff:
                title = r.get("chosen_title", "").strip()
                if title:
                    recent_titles.append(title)
        except (ValueError, TypeError):
            continue

    _RECENT_TITLES_LIST = recent_titles
    _CACHED_SPREADSHEET_ID = spreadsheet_id
    _LAST_FETCH_TIME = now
    print(f"[DEDUP] تم تحميل {len(_RECENT_TITLES_LIST)} عنواناً قديماً من الشيت لفحص التكرار.")


def is_duplicate_topic(title: str, spreadsheet_id: str) -> bool:
    global _OLD_TITLES_EMBEDDINGS_CACHE
    
    # 1. تحميل العناوين القديمة من الشيت مرة واحدة فقط للجلسة الحالية
    _load_recent_titles_once(spreadsheet_id)

    if not _RECENT_TITLES_LIST:
        return False

    # 2. حساب Embedding للعنوان الجديد المراد فحصه (استدعاء واحد فقط)
    try:
        new_embedding = gemini_client.get_embedding(title)
    except Exception as e:
        print(f"[DEDUP ERROR] فشل توليد embedding للعنوان الجديد '{title}': {e}")
        return False  # مسار آمن في حال فشل الـ API

    # 3. مقارنة العنوان الجديد بكافة العناوين القديمة
    for old_title in _RECENT_TITLES_LIST:
        # إذا كان العنوان القديم غير مخزن في الذاكرة المؤقتة، نقوم بحساب الـ Embedding له وحفظه
        if old_title not in _OLD_TITLES_EMBEDDINGS_CACHE:
            try:
                _OLD_TITLES_EMBEDDINGS_CACHE[old_title] = gemini_client.get_embedding(old_title)
            except Exception as e:
                print(f"[DEDUP WARNING] تخطي العنوان القديم '{old_title}' بسبب فشل الـ API: {e}")
                continue

        old_embedding = _OLD_TITLES_EMBEDDINGS_CACHE[old_title]
        
        # حساب نسبة التشابه بين المعنيين
        if _cosine_similarity(new_embedding, old_embedding) >= SIMILARITY_THRESHOLD:
            print(f"[DUPLICATE BLOCKED] تم استبعاد الموضوع '{title}' لتشابهه مع موضوع سابق: '{old_title}'")
            return True
            
    return False
