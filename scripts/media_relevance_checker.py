"""
media_relevance_checker.py
غلاف رفيع فوق analysis_engine.py — يحافظ على نفس اسم الدالة
(verify_media_file) المُستخدم بباقي المشروع لضمان التوافق الخلفي.

كل منطق التحليل الفعلي (الطبقات الخمس: Gemini → Groq → Puter → CLIP
→ قبول تلقائي) انتقل بالكامل إلى scripts/analysis_engine.py كملف
منفصل يُستدعى عند الحاجة — حسب الطلب.
"""
from scripts import analysis_engine


def verify_media_file(file_path: str, narration: str) -> bool:
    """نفس التوقيع والسلوك السابق بالضبط — تفوّض العمل لـ analysis_engine."""
    return analysis_engine.verify(file_path, narration)
