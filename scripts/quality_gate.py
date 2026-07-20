"""
quality_gate.py
يقيّم السكربت قبل الرندرة (بدون مراجعة بشرية، هذا هو البديل الآلي عنها).
لو النتيجة أقل من العتبة -> يرسل تنبيه ويوقف تلك المحاولة (الاستدعاء الأعلى
يقرر إعادة المحاولة مرة واحدة إضافية عبر script_writer ثم يستسلم وينبه).
"""
import json
from scripts import config, gemini_client
from scripts.telegram_alerts import send_alert
from scripts.content_policy import contains_blocked_content

EVAL_PROMPT = """
قيّم سكربت فيديو يوتيوب التالي بصرامة على المعايير التالية (كل معيار من 10):
1. الأصالة (originality) — هل يبدو منسوخاً أو مبتذلاً؟
2. الدقة المفترضة (لا ادعاءات خطيرة/طبية/قانونية قطعية بدون تحفظ)
3. الحساسية (خالٍ تماماً من: سياسة/شخصيات حقيقية مثيرة للجدل، محتوى مضلل،
   أو أي إشارة ولو غير مباشرة لخمر/مخدرات/عري/قمار/محتوى جنسي)
4. جودة الـ hook (هل أول جملة تخلق فضول حقيقي؟)

السكربت:
{script_text}

أرجع JSON فقط:
{{"originality": X, "accuracy": X, "sensitivity": X, "hook_quality": X,
  "average": X, "verdict": "pass" أو "fail", "reason": "..."}}
"""


def evaluate(script_text: str) -> dict:
    # الطبقة الأولى: فحص صارم حتمي (hard block) قبل أي استدعاء لـ Gemini —
    # لا نعتمد فقط على تقييم النموذج الذاتي لمواضيع دينياً/سياسة يوتيوب حساسة
    blocked, category = contains_blocked_content(script_text)
    if blocked:
        send_alert(f"Quality Gate: رفض فوري — كلمة محظورة بفئة '{category}'", level="warning")
        return {
            "originality": 0, "accuracy": 0, "sensitivity": 0, "hook_quality": 0,
            "average": 0, "verdict": "fail",
            "reason": f"يحتوي محتوى محظور بفئة: {category}",
            "passed": False,
        }

    prompt = EVAL_PROMPT.format(script_text=script_text)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_QUALITY_GATE, key_type="light",
        json_mode=True, temperature=0.2,
    )
    result = json.loads(raw)
    result["passed"] = result.get("average", 0) >= config.QUALITY_GATE_MIN_SCORE
    if not result["passed"]:
        send_alert(
            f"Quality Gate رفض السكربت (متوسط {result.get('average')}): "
            f"{result.get('reason')}",
            level="warning",
        )
    return result
