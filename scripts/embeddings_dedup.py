"""
embeddings_dedup.py
يمنع تكرار نفس الموضوع خلال آخر 60 يوم عبر مقارنة embeddings (cosine similarity)
بدل المطابقة النصية البسيطة — أدق بكثير ويحمي من "Inauthentic/Reused Content".
"""
import numpy as np
from datetime import datetime, timedelta

from scripts import config, sheets_client, gemini_client

SIMILARITY_THRESHOLD = 0.87  # فوق هذا الرقم يعتبر نفس الموضوع تقريباً


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def is_duplicate_topic(title: str, spreadsheet_id: str) -> bool:
    try:
        records = sheets_client.get_all_records(spreadsheet_id, config.Paths().sheets_trend_log)
    except Exception:
        return False  # لو الجدول فاضي أو أول تشغيل

    cutoff = datetime.utcnow() - timedelta(days=config.DEDUP_LOOKBACK_DAYS)
    recent_titles = []
    for r in records:
        try:
            row_date = datetime.fromisoformat(str(r.get("date", "")))
            if row_date >= cutoff:
                recent_titles.append(r.get("chosen_title", ""))
        except (ValueError, TypeError):
            continue  # صف بدون تاريخ صالح، تجاهله بدل ما يوقف الفحص كله

    if not recent_titles:
        return False

    new_embedding = gemini_client.get_embedding(title)
    for old_title in recent_titles:
        old_embedding = gemini_client.get_embedding(old_title)
        if _cosine_similarity(new_embedding, old_embedding) >= SIMILARITY_THRESHOLD:
            return True
    return False
