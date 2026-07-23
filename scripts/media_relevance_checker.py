"""
media_relevance_checker.py
غلاف رفيع فوق analysis_engine.py — يحافظ على نفس اسم الدالة
(verify_media_file) المُستخدم بباقي المشروع لضمان التوافق الخلفي.

كل منطق التحليل الفعلي (الطبقات الخمس: Gemini → Groq → Puter → CLIP
→ قبول تلقائي) انتقل بالكامل إلى scripts/analysis_engine.py كملف
منفصل يُستدعى عند الحاجة — حسب الطلب.

تحديث: الآن ترجع قاموساً كاملاً بدل bool فقط — يحتوي على:
  {"passed": bool, "score": float, "breakdown": dict, "layer": str, "model": str}
الكود القديم الذي يتعامل مع النتيجة كـ bool يبقى متوافقاً لأن
dict غير فارغ يُقيَّم دائماً كـ True بلغة بايثون — لكن الاستخدام
الجديد يستخرج result["score"] لاختيار أفضل مرشح بدل قبول أول واحد.
"""
from scripts import analysis_engine


def verify_media_file(file_path: str, narration: str, topic_context: str = "") -> dict:
    """نفس التوقيع السابق مع إضافة topic_context اختياري (المزاج/الهوية
    البصرية الثابتة لكامل الفيديو) — تفوّض العمل لـ analysis_engine.
    ترجع قاموساً كاملاً يحتوي score و passed وتفاصيل أخرى."""
    return analysis_engine.verify(file_path, narration, topic_context)
