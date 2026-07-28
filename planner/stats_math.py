"""
أدوات إحصائية + Multi-Armed Bandit للطبقة "الذكية" في planner/.

هذا الملف مستقل تمامًا عن خط إنتاج الفيديو (main.py/modules/) - رياضيات بحتة
بلا أي اتصال شبكي، تُستخدم فقط من weekly_planner.py لاتخاذ قرارات مبنية على
دلالة إحصائية حقيقية بدل مقارنة متوسطات خام.

لماذا بدون numpy/scipy: البيئة الحالية (requirements.txt) لا تحويهما، وإضافتهما
فقط لأجل اختبار-t تُعتبر مبالغة. الدالتان _betacf/_betainc أدناه هما التطبيق
القياسي (Numerical Recipes) لدالة Beta غير المكتملة المنظّمة، وتُستخدمان لحساب
قيمة-p الدقيقة لتوزيع Student-t دون أي مكتبة خارجية.
"""

import math
import random


# ============================================================
# دالة Beta غير المكتملة المنظّمة (لحساب قيمة-p لتوزيع t بدقة)
# ============================================================

def _betacf(a: float, b: float, x: float, max_iter: int = 200, eps: float = 3e-12) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_tailed_p_value(t: float, df: float) -> float:
    """قيمة-p ثنائية الطرف لإحصائية t بدرجات حرية df (توزيع Student-t)."""
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    p = _betainc(df / 2.0, 0.5, x)
    return max(0.0, min(1.0, p))


# ============================================================
# اختبار Welch's t-test (لا يفترض تساوي التباين أو حجم العيّنة)
# ============================================================

def welch_t_test(sample_a: list, sample_b: list, alpha: float = 0.05) -> dict:
    """يقارن عيّنتين رقميتين (مثلاً composite_score لكل فيديو في فئتين) ويعيد
    ما إذا كان الفرق بينهما دالًا إحصائيًا (وليس مجرد ضجيج عشوائي)."""
    n_a, n_b = len(sample_a), len(sample_b)
    mean_a = sum(sample_a) / n_a if n_a else 0.0
    mean_b = sum(sample_b) / n_b if n_b else 0.0

    if n_a < 2 or n_b < 2:
        return {
            "t": 0.0, "df": 0.0, "p_value": 1.0, "significant": False,
            "mean_a": mean_a, "mean_b": mean_b, "n_a": n_a, "n_b": n_b,
            "reason": "insufficient_sample",
        }

    var_a = sum((x - mean_a) ** 2 for x in sample_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in sample_b) / (n_b - 1)
    se_sq = var_a / n_a + var_b / n_b
    se = math.sqrt(se_sq) if se_sq > 0 else 0.0
    t = (mean_a - mean_b) / se if se > 0 else 0.0

    if var_a == 0 and var_b == 0:
        df = n_a + n_b - 2
    else:
        num = se_sq ** 2
        den = ((var_a / n_a) ** 2) / (n_a - 1) + ((var_b / n_b) ** 2) / (n_b - 1)
        df = num / den if den > 0 else (n_a + n_b - 2)

    p_value = t_two_tailed_p_value(t, df)
    return {
        "t": round(t, 4), "df": round(df, 2), "p_value": round(p_value, 5),
        "significant": p_value < alpha,
        "mean_a": round(mean_a, 3), "mean_b": round(mean_b, 3),
        "n_a": n_a, "n_b": n_b, "reason": None,
    }


def mean(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1))


# ============================================================
# درجة مركّبة (composite score) لكل فيديو
# ============================================================

def composite_score(views: float, likes: float, comments: float,
                     avg_view_percentage: float = None) -> float:
    """درجة واحدة قابلة للمقارنة بين الفيديوهات، تُستخدم كوحدة قياس موحّدة لكل
    التعلّم (تصنيف فئات/مواضيع/أوقات نشر) بدل الاعتماد على views الخام وحدها.

    - إن توفّرت نسبة الاحتفاظ الحقيقية (retention عبر Analytics API) فهي المكوّن
      المهيمن (55%) لأنها أفضل مؤشر معروف لجودة السكربت/الهوك نفسه، بمعزل عن
      حظ التوزيع العشوائي من خوارزمية يوتيوب.
    - نسبة تفاعل (likes + comments*2) / views تعكس مدى "إثارة" المحتوى فعليًا
      (التعليق إشارة أقوى من اللايك فوزّن ×2).
    - log10(views) مكوّن صغير (10-15%) فقط، لتفادي هيمنة فيديو واحد "فيرال
      بالصدفة" على التقييم الكلي كما ورد في الملاحظة الأصلية.

    هذه صيغة استدلالية (heuristic) وليست مثبتة أكاديميًا - الهدف توحيد القياس
    وليس الادّعاء بدقة علمية مطلقة.
    """
    views = max(float(views or 0), 0.0)
    likes = max(float(likes or 0), 0.0)
    comments = max(float(comments or 0), 0.0)
    engagement_rate = ((likes + comments * 2.0) / views) if views > 0 else 0.0
    engagement_component = min(engagement_rate * 100.0, 100.0)  # يُحدّ عند 100 لمنع القيم الشاذة
    reach_component = math.log10(views + 1.0) * 10.0

    if avg_view_percentage is not None:
        avg_view_percentage = max(0.0, min(float(avg_view_percentage), 100.0))
        return round(0.55 * avg_view_percentage + 0.30 * engagement_component + 0.15 * reach_component, 3)
    return round(0.65 * engagement_component + 0.35 * reach_component, 3)


# ============================================================
# Epsilon-greedy multi-armed bandit (لأوقات النشر وفئات الاستكشاف)
# ============================================================

def epsilon_greedy_pick(arm_scores: dict, epsilon: float, optimistic_default: float = 60.0,
                         rng: random.Random = None) -> str:
    """يختار "ذراعًا" واحدة (وقت نشر/فئة) من قاموس {arm: متوسط_الدرجة}.

    - أذرع بلا بيانات كافية (غير موجودة في القاموس أو None) تُعطى قيمة متفائلة
      افتراضية (optimistic_default) بدل صفر، لتُجرَّب على الأقل مرة بدل إقصائها
      أبديًا لمجرد عدم وجود بيانات عنها بعد (مشكلة "cold start").
    - بنسبة epsilon: استكشاف عشوائي بحت (يضمن استمرار اكتشاف تحوّلات الذوق حتى
      بعد استقرار الأداء - هذا هو جوهر حل مشكلة "لا تعلّم بعد القفل").
    - غير ذلك: استغلال أفضل ذراع معروفة حاليًا.
    """
    rng = rng or random
    if not arm_scores:
        raise ValueError("arm_scores فارغة")
    if rng.random() < epsilon:
        return rng.choice(list(arm_scores.keys()))
    scored = {k: (v if v is not None else optimistic_default) for k, v in arm_scores.items()}
    return max(scored.items(), key=lambda kv: kv[1])[0]


def weighted_sample_without_replacement(arm_scores: dict, k: int, epsilon: float,
                                         optimistic_default: float = 60.0,
                                         rng: random.Random = None) -> list:
    """يسحب k ذراعًا بلا تكرار (مثلاً: أفضل k وقت نشر لليوم الواحد)، بتطبيق
    نفس منطق epsilon-greedy لكل سحبة على التوالي من الأذرع المتبقية."""
    rng = rng or random
    remaining = dict(arm_scores)
    picks = []
    for _ in range(min(k, len(remaining))):
        pick = epsilon_greedy_pick(remaining, epsilon, optimistic_default, rng)
        picks.append(pick)
        del remaining[pick]
    return picks
