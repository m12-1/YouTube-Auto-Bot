"""
المخطط الأسبوعي/الشهري + طبقة "التعلّم" الفعلية للنظام.

الفرق الجوهري عن النسخة السابقة (كانت: قواعد ثابتة + قرار تحليلي واحد يُقفل
للأبد): هذه النسخة نظام تعلّم مستمر حقيقي بمعنى تشغيلي بسيط:

1. **القفل على الفئات مبني على دلالة إحصائية حقيقية** (Welch's t-test على
   composite_score لكل فيديو، وليس مجرد مقارنة متوسطات خام)، ولا يُقفل قبل
   حجم عيّنة كافٍ لكل فئة (planner/config_planner.py -> MIN_VIDEOS_PER_CATEGORY_BEFORE_LOCK).
2. **لا استغلال بحت بعد القفل**: نسبة EXPLORATION_RATE من كل خطة شهرية تذهب
   لفئة "تجريبية" (challenger) تدور على الفئات غير المقفلة، ودوريًا
   (RE_EVALUATION_INTERVAL_DAYS) تُقارَن إحصائيًا بأضعف فئة مقفلة - إن تفوّقت
   بدلالة إحصائية، تستبدلها تلقائيًا. هذا يكتشف تحوّل ذوق الجمهور لاحقًا.
3. **تعلّم على مستوى الموضوع/الزاوية**: كل طلب مواضيع جديد لجمناي يتضمن أفضل/
   أسوأ 5 مواضيع أداءً (بالاسم والدرجة) وأداء كل "زاوية/نمط هوك" (angle) كأمثلة
   صريحة، بدل الاعتماد فقط على "تجنّب التكرار".
4. **تعلّم على وقت النشر**: بدل وقتين ثابتين للأبد، بنك من الأوقات المرشّحة
   ونظام epsilon-greedy bandit (planner/stats_math.py) يختار الأوقات يوميًا
   حسب composite_score التاريخي لكل نافذة، مع استكشاف عشوائي دائم.

هذا الملف يبقى مستقلًا تمامًا عن main.py (خط إنتاج الفيديو) تمامًا كالنسخة
الأصلية - لا تغيير هنا يمسّ الرندر/التحقق من المشاهد/التدقيق بأي شكل.
"""

import json
import logging
import random
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

from planner.config_planner import (
    TAB_CONFIG, TAB_PLAN, TAB_STATS, TEST_WAVES, CATEGORY_LABELS, CATEGORY_POOL,
    CONTENT_SAFETY_RULES, VIDEOS_PER_DAY, DAYS_PER_WEEK, VIDEOS_PER_WEEK,
    US_TIMEZONE, CANDIDATE_SLOT_TIMES_ET, SLOT_EXPLORATION_EPSILON, SLOT_JITTER_MINUTES,
    STAGE_PLANNER, STAGE_ANALYZER,
    MIN_VIDEOS_PER_CATEGORY_BEFORE_LOCK, MAX_TEST_WEEKS, SIGNIFICANCE_ALPHA,
    EXPLORATION_RATE, RE_EVALUATION_INTERVAL_DAYS, MIN_VIDEOS_PER_CHALLENGER_BEFORE_SWAP,
    RE_EVALUATION_WINDOW_DAYS,
)
from planner import sheets_client, stats_math
from shared.gemini_client import call_gemini_with_rotation, parse_json_response

logger = logging.getLogger("planner.weekly_planner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ================================================================
# Config tab
# ================================================================

def _load_config() -> dict:
    rows = sheets_client.read_all(TAB_CONFIG)
    if rows:
        return rows[0]
    default_values = ["0", "", "FALSE", "", "", "", "0", "", ""]
    sheets_client.append_rows(TAB_CONFIG, [default_values])
    return {
        "week_number": "0", "active_categories": "", "locked": "FALSE",
        "last_updated_utc": "", "tested_history": "", "last_reevaluation_utc": "",
        "challenger_rotation_index": "0", "swap_log": "", "current_challenger": "", "_row_number": 2,
    }


def _save_config(cfg: dict):
    sheets_client.update_row(TAB_CONFIG, int(cfg["_row_number"]), [
        cfg.get("week_number", "0"),
        cfg.get("active_categories", ""),
        cfg.get("locked", "FALSE"),
        datetime.now(timezone.utc).isoformat(),
        cfg.get("tested_history", ""),
        cfg.get("last_reevaluation_utc", ""),
        cfg.get("challenger_rotation_index", "0"),
        cfg.get("swap_log", ""),
        cfg.get("current_challenger", ""),
    ])


# ================================================================
# Plan tab helpers
# ================================================================

def _plan_rows():
    return sheets_client.read_all(TAB_PLAN)


def _has_unfinished(rows) -> bool:
    return any(r.get("status") in ("pending", "in_progress") for r in rows)


def _all_topics_ever_used(plan_rows) -> list:
    return [r["topic"] for r in plan_rows if r.get("topic")]


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ================================================================
# Stats tab -> composite scores (وحدة القياس الموحّدة لكل التعلّم)
# ================================================================

def _stats_rows_with_score():
    """يقرأ Stats ويعيد كل صف مع composite_score محسوبة (يتجاهل الصفوف بلا
    بيانات views_72h بعد - لم يستحق فحصها بعد)."""
    rows = sheets_client.read_all(TAB_STATS)
    out = []
    for r in rows:
        if not r.get("views_72h"):
            continue  # لم تكتمل نافذة الـ72 ساعة بعد لهذا الفيديو
        avg_pct = _to_float(r.get("avg_view_percentage_72h"), default=None) \
            if r.get("avg_view_percentage_72h") not in (None, "") else None
        score = stats_math.composite_score(
            views=_to_float(r.get("views_72h")),
            likes=_to_float(r.get("likes_72h")),
            comments=_to_float(r.get("comments_72h")),
            avg_view_percentage=avg_pct,
        )
        r["_composite_score"] = score
        out.append(r)
    return out


def _scores_by_category(scored_rows) -> dict:
    by_cat = {}
    for r in scored_rows:
        cat = r.get("category")
        if not cat:
            continue
        by_cat.setdefault(cat, []).append(r["_composite_score"])
    return by_cat


def _recent_scored_rows(days: int):
    """نسخة من _stats_rows_with_score() مقيّدة بآخر `days` يومًا فقط - تُستخدم
    في إعادة التقييم بعد القفل حتى لا يُخفّف أداء قديم أي إشارة تحوّل حديثة
    فعلية في ذوق الجمهور (راجع RE_EVALUATION_WINDOW_DAYS في config_planner.py)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for r in _stats_rows_with_score():
        published_at = r.get("published_at_utc")
        try:
            if published_at and datetime.fromisoformat(published_at) >= cutoff:
                out.append(r)
        except ValueError:
            continue
    return out


# ================================================================
# Gemini: اختيار المواضيع (مع تغذية أفضل/أسوأ أداء كأمثلة صريحة - نقطة 3)
# ================================================================

def _top_bottom_topics(scored_rows, n=5) -> tuple:
    ranked = sorted(scored_rows, key=lambda r: r["_composite_score"], reverse=True)
    top = [{"topic": r["topic"], "category": r["category"], "score": r["_composite_score"]} for r in ranked[:n]]
    bottom = [{"topic": r["topic"], "category": r["category"], "score": r["_composite_score"]} for r in ranked[-n:]]
    return top, bottom


def _angle_performance(scored_rows) -> dict:
    """متوسط composite_score لكل "زاوية/نمط هوك" مسجّل سابقًا - يسمح لجمناي
    برؤية أي أنماط عناوين/زوايا نجحت فعليًا، لا فقط أي فئة."""
    by_angle = {}
    for r in scored_rows:
        angle = (r.get("angle") or "").strip()
        if not angle:
            continue
        by_angle.setdefault(angle, []).append(r["_composite_score"])
    return {
        angle: {"avg_score": round(stats_math.mean(v), 2), "n": len(v)}
        for angle, v in by_angle.items() if len(v) >= 3  # تجاهل أنماط بعيّنة أصغر من أن تعني شيئًا
    }


def _ask_gemini_for_topics(categories: list, count: int, avoid_topics: list) -> list:
    labels = {c: CATEGORY_LABELS[c] for c in categories}
    avoid_clause = ""
    if avoid_topics:
        recent_avoid = avoid_topics[-150:]  # لا نضخّم البرومبت بلا داعٍ
        avoid_clause = "Avoid these exact topics, already used before:\n- " + "\n- ".join(recent_avoid)

    scored_rows = _stats_rows_with_score()
    learning_clause = ""
    if scored_rows:
        top, bottom = _top_bottom_topics(scored_rows)
        angle_perf = _angle_performance(scored_rows)
        learning_clause = f"""
Learn from real published-video performance (composite_score blends retention,
engagement rate, and reach - higher is better):

Best-performing topics so far (study WHY these worked - specific angle, phrasing, hook):
{json.dumps(top, indent=2)}

Worst-performing topics so far (avoid this style/angle, not just these exact topics):
{json.dumps(bottom, indent=2)}
"""
        if angle_perf:
            learning_clause += f"""
Average performance per "angle"/hook style tagged on past topics (prefer angles
with higher avg_score when choosing a new angle for each topic):
{json.dumps(angle_perf, indent=2)}
"""

    prompt = f"""
You are a content-planning assistant for an English-language, brand-safe, faceless
YouTube Shorts channel that sources footage from Pexels/Pixabay stock libraries.

{CONTENT_SAFETY_RULES}

Categories to plan for (use the exact key when returning JSON):
{json.dumps(labels, indent=2)}

Generate exactly {count} distinct, specific video topics, distributed as evenly as
possible across the given categories. Each topic must be specific and interesting
enough to sustain a ~45-55 second script without feeling generic, and must have a
clear angle/hook potential (a surprising fact, a tension, a "wait, what?" element).
For each topic, also assign a short "angle" tag (2-4 words, e.g. "surprising_fact",
"myth_busting", "scale_comparison", "how_it_works", "hidden_danger", "countdown_list")
describing the hook style used, so performance can be tracked per style over time.
{avoid_clause}
{learning_clause}

Return ONLY a JSON object in this exact shape, no extra text or Markdown fences:
{{
  "topics": [
    {{"category": "space", "topic": "...", "angle": "surprising_fact"}}
  ]
}}
The list must contain exactly {count} items.
"""
    raw = call_gemini_with_rotation(
        STAGE_PLANNER, [prompt], response_mime_type="application/json",
        max_output_tokens=4096, temperature=0.9,
    )
    data = parse_json_response(raw)
    topics = data.get("topics", [])
    if len(topics) != count:
        logger.warning("جمناي أعاد %d موضوعًا بدل %d المطلوبة - سيُستخدم المتاح فقط.", len(topics), count)
    return topics[:count]


# ================================================================
# القفل الإحصائي على الفئات (نقطة 1) - القرار الحاسم يتخذه الكود لا جمناي
# ================================================================

def _ready_to_lock(week_number: int) -> tuple:
    """يعيد (جاهز؟, أفضل_3_فئات, تفاصيل_للتسجيل). لا يقفل أبدًا لمجرد انتهاء
    عدد أسابيع ثابت - فقط عند: عيّنة كافية لكل فئة + فرق دالّ إحصائيًا بين
    الفئة الثالثة والرابعة، أو بلوغ سقف أمان MAX_TEST_WEEKS."""
    scored_rows = _stats_rows_with_score()
    by_cat = _scores_by_category(scored_rows)

    tested = set()
    for group in TEST_WAVES:
        tested.update(group)
    samples_ok = {cat: len(by_cat.get(cat, [])) for cat in tested}
    min_sample_reached = all(n >= MIN_VIDEOS_PER_CATEGORY_BEFORE_LOCK for n in samples_ok.values())

    if not by_cat:
        return False, None, {"reason": "no_data_yet"}

    ranked = sorted(by_cat.items(), key=lambda kv: stats_math.mean(kv[1]), reverse=True)
    ranked = [(cat, scores) for cat, scores in ranked if cat in tested]

    hit_safety_cap = week_number >= MAX_TEST_WEEKS

    if not min_sample_reached and not hit_safety_cap:
        return False, None, {"reason": "insufficient_sample", "samples": samples_ok}

    if len(ranked) < 4:
        # أقل من 4 فئات لها بيانات كافية للمقارنة - لا معنى لاختبار الدلالة،
        # نأخذ أفضل ما هو متاح (يحدث فقط لو كانت التغطية غير مكتملة أصلًا)
        chosen = [c for c, _ in ranked[:3]]
        return True, chosen, {"reason": "not_enough_categories_for_significance_test", "ranked_means":
                               {c: round(stats_math.mean(s), 2) for c, s in ranked}}

    third_scores = ranked[2][1]
    fourth_scores = ranked[3][1]
    test_result = stats_math.welch_t_test(third_scores, fourth_scores, alpha=SIGNIFICANCE_ALPHA)

    if test_result["significant"] or hit_safety_cap:
        chosen = [c for c, _ in ranked[:3]]
        details = {
            "reason": "significant_gap" if test_result["significant"] else "safety_cap_reached_without_significance",
            "significance_test_rank3_vs_rank4": test_result,
            "ranked_means": {c: round(stats_math.mean(s), 2) for c, s in ranked},
        }
        return True, chosen, details

    return False, None, {
        "reason": "gap_between_rank3_and_rank4_not_significant_yet",
        "significance_test_rank3_vs_rank4": test_result,
        "ranked_means": {c: round(stats_math.mean(s), 2) for c, s in ranked},
    }


def _run_category_lock(cfg: dict, week_number: int) -> list:
    ready, chosen, details = _ready_to_lock(week_number)
    if not ready:
        return None
    logger.info("قرار القفل على الفئات (مبني على دلالة إحصائية Welch's t-test): %s", details)

    # نطلب من جمناي فقط شرحًا بشريًا مختصرًا للتسجيل - القرار الرقمي نفسه
    # اتُّخذ أعلاه بالكامل بالكود، وليس بقراءة جمناي لمتوسطات خام (نقطة 1).
    try:
        prompt = f"""
A 3-category shortlist was already chosen statistically (Welch's t-test) from
performance data below. Write ONE short sentence (max 25 words) explaining the
likely content reason these categories may resonate, for an internal log only.
Chosen categories: {chosen}
Supporting stats: {json.dumps(details, indent=2, default=str)}
Return ONLY a JSON object: {{"reasoning": "..."}}
"""
        raw = call_gemini_with_rotation(
            STAGE_ANALYZER, [prompt], response_mime_type="application/json",
            max_output_tokens=256, temperature=0.4,
        )
        reasoning = parse_json_response(raw).get("reasoning", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("تعذّر الحصول على شرح نصي من جمناي (لا يؤثر على القرار نفسه): %s", e)
        reasoning = ""

    logger.info("تم القفل إحصائيًا على: %s | %s", chosen, reasoning)
    return chosen


# ================================================================
# Scheduling + bandit على أوقات النشر (نقطة 4)
# ================================================================

def _slot_bucket_scores() -> dict:
    """متوسط composite_score لكل نافذة زمنية مرشّحة، من واقع الأداء الفعلي
    المسجَّل في Stats (عمود slot_bucket)."""
    scored_rows = _stats_rows_with_score()
    by_bucket = {}
    for r in scored_rows:
        bucket = r.get("slot_bucket")
        if bucket not in CANDIDATE_SLOT_TIMES_ET:
            continue
        by_bucket.setdefault(bucket, []).append(r["_composite_score"])
    return {b: (stats_math.mean(v) if v else None) for b, v in by_bucket.items()}


def _scheduled_slots(start_date: date, num_days: int) -> list:
    """يبني (تاريخ، وقت_ET، datetime_utc، slot_bucket) لكل الأيام المطلوبة.
    الوقت اليومي يُختار عبر epsilon-greedy bandit من CANDIDATE_SLOT_TIMES_ET
    حسب الأداء التاريخي لكل نافذة (بدل وقتين ثابتين للأبد)."""
    slots = []
    tz = ZoneInfo(US_TIMEZONE)
    bucket_scores = _slot_bucket_scores()
    rng = random.Random()

    for day_offset in range(num_days):
        the_date = start_date + timedelta(days=day_offset)
        chosen_buckets = stats_math.weighted_sample_without_replacement(
            {b: bucket_scores.get(b) for b in CANDIDATE_SLOT_TIMES_ET},
            k=VIDEOS_PER_DAY, epsilon=SLOT_EXPLORATION_EPSILON, rng=rng,
        )
        for slot_str in sorted(chosen_buckets):
            hh, mm = map(int, slot_str.split(":"))
            jitter = random.randint(-SLOT_JITTER_MINUTES, SLOT_JITTER_MINUTES)
            local_dt = datetime(the_date.year, the_date.month, the_date.day, hh, mm, tzinfo=tz) \
                + timedelta(minutes=jitter)
            utc_dt = local_dt.astimezone(timezone.utc)
            slots.append((the_date.isoformat(), local_dt.strftime("%H:%M"), utc_dt.isoformat(), slot_str))
    return slots


def _write_plan_rows(topics: list, slots: list, is_exploration_flags: list = None):
    rows = []
    is_exploration_flags = is_exploration_flags or [False] * len(topics)
    for i, (topic_item, slot) in enumerate(zip(topics, slots)):
        the_date, time_et, dt_utc, slot_bucket = slot
        row_id = f"{the_date}_{time_et.replace(':', '')}_{i}"
        rows.append([
            row_id, topic_item.get("category", ""), topic_item.get("topic", ""),
            the_date, time_et, dt_utc, "pending", "", "",
            slot_bucket, "TRUE" if is_exploration_flags[i] else "FALSE",
            topic_item.get("angle", ""),
        ])
    sheets_client.append_rows(TAB_PLAN, rows)
    logger.info("تمت إضافة %d صف/صفوف جديدة لجدول Plan.", len(rows))


# ================================================================
# استكشاف مستمر بعد القفل (نقطة 2) - فئة تجريبية + إعادة تقييم دورية
# ================================================================

def _next_challenger_category(cfg: dict, locked_categories: list, recent_by_cat: dict) -> tuple:
    """يختار الفئة التجريبية لهذا الشهر. الفرق الجوهري عن نسخة سابقة: **يبقى**
    على نفس الفئة التجريبية الحالية طالما لم تجمع بعد عيّنة حديثة كافية
    (ضمن نافذة RE_EVALUATION_WINDOW_DAYS) لمقارنتها إحصائيًا - بدل الانتقال
    شهريًا بلا اعتبار لكفاية البيانات، ما كان يمنع أي فئة من الوصول فعليًا
    لعتبة المقارنة قبل أن يُنتقل عنها."""
    candidates = [c for c in CATEGORY_POOL if c not in locked_categories]
    if not candidates:
        return None, cfg

    current = cfg.get("current_challenger") or ""
    if current in candidates:
        sample_n = len(recent_by_cat.get(current, []))
        if sample_n < MIN_VIDEOS_PER_CHALLENGER_BEFORE_SWAP:
            return current, cfg  # ما زلنا نجمع عيّنة حديثة كافية لنفس الفئة - لا نتنقّل بعد

    idx = int(cfg.get("challenger_rotation_index") or 0) % len(candidates)
    chosen = candidates[idx]
    cfg["challenger_rotation_index"] = str((idx + 1) % len(candidates))
    cfg["current_challenger"] = chosen
    return chosen, cfg


def _maybe_swap_weak_category(cfg: dict, locked_categories: list, recent_by_cat: dict) -> list:
    """يقارن الفئة التجريبية الحالية بأضعف فئة مقفلة، باستخدام بيانات آخر
    RE_EVALUATION_WINDOW_DAYS يومًا فقط (وليس كل التاريخ) حتى يبقى حسّاسًا
    لتحوّل ذوق حديث بدل أن يُخفّفه أداء قديم (نقطة 2 من الملاحظات الأصلية)."""
    last_reeval = cfg.get("last_reevaluation_utc")
    now = datetime.now(timezone.utc)
    if last_reeval:
        try:
            last_dt = datetime.fromisoformat(last_reeval)
            if (now - last_dt).days < RE_EVALUATION_INTERVAL_DAYS:
                return locked_categories  # لم يحن وقت إعادة التقييم بعد
        except ValueError:
            pass

    locked_means = {c: stats_math.mean(recent_by_cat.get(c, [])) for c in locked_categories if recent_by_cat.get(c)}
    cfg["last_reevaluation_utc"] = now.isoformat()
    if not locked_means:
        return locked_categories
    weakest_cat = min(locked_means.items(), key=lambda kv: kv[1])[0]
    weakest_scores = recent_by_cat.get(weakest_cat, [])

    challenger_cats = [c for c in recent_by_cat if c not in locked_categories]
    best_swap = None
    for challenger in challenger_cats:
        challenger_scores = recent_by_cat[challenger]
        if len(challenger_scores) < MIN_VIDEOS_PER_CHALLENGER_BEFORE_SWAP:
            continue
        test = stats_math.welch_t_test(challenger_scores, weakest_scores, alpha=SIGNIFICANCE_ALPHA)
        if test["significant"] and test["mean_a"] > test["mean_b"]:
            if best_swap is None or test["mean_a"] > best_swap[2]:
                best_swap = (challenger, test, test["mean_a"])

    if best_swap:
        challenger, test, _ = best_swap
        new_categories = [c for c in locked_categories if c != weakest_cat] + [challenger]
        log_entry = (f"{now.isoformat()}: استُبدلت '{weakest_cat}' (avg آخر {RE_EVALUATION_WINDOW_DAYS} يومًا="
                     f"{round(stats_math.mean(weakest_scores), 2)}) بـ '{challenger}' "
                     f"(avg={test['mean_a']}, p={test['p_value']})")
        prior_log = cfg.get("swap_log") or ""
        cfg["swap_log"] = (prior_log + " | " + log_entry) if prior_log else log_entry
        cfg["active_categories"] = ",".join(new_categories)
        cfg["current_challenger"] = ""  # الفئة التجريبية التي فازت أصبحت الآن مقفلة - نبدأ اختيار تجريبية جديدة
        logger.info("استبدال فئة تلقائي بعد إعادة التقييم الدورية: %s", log_entry)
        return new_categories

    logger.info("إعادة التقييم الدورية (آخر %d يومًا): لا فئة تجريبية تفوّقت إحصائيًا بعد على أضعف فئة مقفلة (%s).",
                RE_EVALUATION_WINDOW_DAYS, weakest_cat)
    return locked_categories


def _build_monthly_plan(cfg: dict, avoid_topics: list, num_days: int = 30):
    locked_categories = [c for c in (cfg.get("active_categories") or "").split(",") if c]
    if not locked_categories:
        locked_categories = TEST_WAVES[-1]

    recent_by_cat = _scores_by_category(_recent_scored_rows(RE_EVALUATION_WINDOW_DAYS))
    locked_categories = _maybe_swap_weak_category(cfg, locked_categories, recent_by_cat)
    # لو حدث استبدال، الفئة الفائزة خرجت من قائمة "المرشّحين" وقد تغيّرت الحسابات -
    # نعيد احتساب recent_by_cat لضمان اتساق الفئات المرشّحة للاستكشاف التالية
    recent_by_cat = _scores_by_category(_recent_scored_rows(RE_EVALUATION_WINDOW_DAYS))

    total_count = num_days * VIDEOS_PER_DAY
    exploration_count = max(1, round(total_count * EXPLORATION_RATE)) if total_count else 0
    locked_count = total_count - exploration_count

    challenger_cat, cfg = _next_challenger_category(cfg, locked_categories, recent_by_cat)

    logger.info(
        "بناء خطة شهرية (%d فيديو): %d بالفئات المقفلة %s + %d استكشاف بفئة '%s' (تعلّم مستمر، ليس استغلالًا بحتًا).",
        total_count, locked_count, locked_categories, exploration_count, challenger_cat,
    )

    topics = _ask_gemini_for_topics(locked_categories, locked_count, avoid_topics)
    is_exploration_flags = [False] * len(topics)

    if challenger_cat and exploration_count > 0:
        challenger_topics = _ask_gemini_for_topics([challenger_cat], exploration_count, avoid_topics)
        topics += challenger_topics
        is_exploration_flags += [True] * len(challenger_topics)

    slots = _scheduled_slots(date.today(), num_days)
    # نخلط ترتيب الاستكشاف بشكل عشوائي بدل تكديسه كله بآخر الشهر
    combined = list(zip(topics, is_exploration_flags))
    random.shuffle(combined)
    topics, is_exploration_flags = ([t for t, _ in combined], [e for _, e in combined]) if combined else ([], [])

    _write_plan_rows(topics, slots[:len(topics)], is_exploration_flags)
    cfg["active_categories"] = ",".join(locked_categories)
    return cfg


# ================================================================
# Orchestration
# ================================================================

def ensure_plan_has_content():
    sheets_client.ensure_sheet_structure()
    cfg = _load_config()
    plan_rows = _plan_rows()

    if _has_unfinished(plan_rows):
        logger.info("توجد خطة سابقة لم تنتهِ بعد - لا حاجة لبناء خطة جديدة الآن.")
        return

    week_number = int(cfg.get("week_number") or 0) + 1
    locked = str(cfg.get("locked")).strip().upper() == "TRUE"
    avoid_topics = _all_topics_ever_used(plan_rows)

    if not locked:
        chosen = _run_category_lock(cfg, week_number)
        if chosen is not None:
            cfg["active_categories"] = ",".join(chosen)
            cfg["locked"] = "TRUE"
            cfg["week_number"] = str(week_number)
            cfg["last_reevaluation_utc"] = datetime.now(timezone.utc).isoformat()
            _save_config(cfg)
            logger.info("تم القفل على الفئات النهائية: %s - بناء أول خطة شهرية.", chosen)
            cfg = _build_monthly_plan(cfg, avoid_topics)
            _save_config(cfg)
            return

        # لم يحن القفل بعد - استمرار بأسبوع اختبار جديد ضمن دورة TEST_WAVES
        # (تتكرر الدورة تلقائيًا حتى تتحقق الدلالة الإحصائية أو سقف الأمان)
        group_index = (week_number - 1) % len(TEST_WAVES)
        categories = TEST_WAVES[group_index]
        logger.info("بناء خطة أسبوع الاختبار رقم %d بالفئات: %s (لم تتحقق الدلالة الإحصائية بعد).",
                    week_number, categories)
        topics = _ask_gemini_for_topics(categories, VIDEOS_PER_WEEK, avoid_topics)
        slots = _scheduled_slots(date.today(), DAYS_PER_WEEK)
        _write_plan_rows(topics, slots[:len(topics)])

        history = set(filter(None, (cfg.get("tested_history") or "").split(",")))
        history.update(categories)
        cfg["week_number"] = str(week_number)
        cfg["tested_history"] = ",".join(sorted(history))
        _save_config(cfg)
        return

    # مقفلة بالفعل: أي مرة تنتهي فيها الخطة الشهرية الحالية نبني شهرًا جديدًا -
    # بالفئات المقفلة + شريحة استكشاف + فحص دوري لاستبدال أضعف فئة إن لزم
    cfg = _build_monthly_plan(cfg, avoid_topics)
    cfg["week_number"] = str(week_number)
    _save_config(cfg)


if __name__ == "__main__":
    ensure_plan_has_content()
