# طبقة التخطيط والتعلّم (planner/)

مستقلة تمامًا عن `main.py` وباقي `modules/` (الرندر/التحقق من المشاهد/التدقيق لم
يتغيّر فيها أي سطر). هذه الطبقة هي فقط "الدماغ" الذي يقرر: أي فئة/موضوع/وقت
نشر، ويتعلّم من الأداء الفعلي بدل الاعتماد على قواعد ثابتة أبدية.

| الملف | المهمة | الجدولة |
|---|---|---|
| `planner/weekly_planner.py` | يبني الخطة، يقرر القفل على الفئات إحصائيًا، يدير الاستكشاف المستمر بعد القفل | كل 6 ساعات |
| `run_from_plan.py` (جذر المشروع) | يلتقط أقرب صف مستحق من `Plan`، يشغّل `main.py` كاملاً، يسجّل صف في `Stats` | كل 15 دقيقة |
| `planner/stats_updater.py` | يملأ views/likes/comments عند 24h/48h/72h + retention عبر Analytics API | كل ساعة |
| `planner/stats_math.py` | رياضيات بحتة: اختبار Welch's t-test، composite_score، bandit epsilon-greedy | بلا شبكة - وحدات مساعدة فقط |
| `planner/analytics_client.py` | يجلب نسبة الاحتفاظ الحقيقية بالمشاهد (retention) عبر YouTube Analytics API | يُستدعى من stats_updater.py |

## ما الذي تغيّر عن التصميم الأول (ولماذا)

كان التصميم الأول: قواعد ثابتة + قرار تحليلي واحد يُقفل للأبد. لم يكن نظام
تعلّم مستمر. التغييرات التالية تحوّله لنظام يقيس فعلًا ما هو الأفضل/الأسوأ
ويُحسّن نفسه:

1. **دلالة إحصائية حقيقية بدل مقارنة متوسطات خام.** `_ready_to_lock()` في
   `weekly_planner.py` لا يقفل على 3 فئات إلا بعد: (أ) عيّنة كافية لكل فئة
   (`MIN_VIDEOS_PER_CATEGORY_BEFORE_LOCK` = 28 فيديو، أي ~6 أسابيع تقويمية
   بدل أسبوع واحد/14 فيديو سابقًا)، و(ب) فرق دالّ إحصائيًا (Welch's t-test،
   p < 0.05) بين الفئة الثالثة والرابعة - وإلا يستمر الاختبار لجولة إضافية
   تلقائيًا حتى سقف أمان `MAX_TEST_WEEKS` = 12 أسبوعًا. القرار الرقمي بالكامل
   في الكود (`planner/stats_math.py`)، وجمناي لا يُستخدم إلا لكتابة جملة شرح
   نصية للسجلّات - لا يقارن هو الأرقام.

2. **لا استغلال بحت بعد القفل.** كل خطة شهرية بعد القفل تخصّص `EXPLORATION_RATE`
   (10% افتراضيًا) لفئة "تجريبية" (challenger) تدور تلقائيًا على الفئات
   الست غير المقفلة (`_next_challenger_category`). كل `RE_EVALUATION_INTERVAL_DAYS`
   (30 يومًا افتراضيًا)، `_maybe_swap_weak_category` يقارن كل فئة تجريبية
   بأضعف فئة مقفلة حاليًا (Welch's t-test مجددًا)؛ إن تفوّقت بدلالة إحصائية
   وبعيّنة ≥ `MIN_VIDEOS_PER_CHALLENGER_BEFORE_SWAP` (15)، تستبدلها تلقائيًا
   ويُسجَّل السبب في `Config.swap_log`. هذا يكتشف تحوّل ذوق الجمهور لاحقًا
   بدل التجمّد على قرار قديم.

3. **تعلّم على مستوى الموضوع/الزاوية.** كل طلب مواضيع جديد لجمناي
   (`_ask_gemini_for_topics`) يتضمن أفضل/أسوأ 5 مواضيع أداءً بالاسم والدرجة،
   وأداء كل "زاوية/نمط هوك" (`angle` - وسم مثل `surprising_fact`/`myth_busting`
   يُطلب من جمناي نفسه لكل موضوع جديد) كأمثلة صريحة، بدل الاعتماد فقط على
   "تجنّب التكرار الحرفي". هذا تعلّم-محتوى فعلي، وليس مجرد فلترة.

4. **بنك أوقات نشر + bandit بدل وقتين ثابتين للأبد.** `CANDIDATE_SLOT_TIMES_ET`
   (6 نوافذ مرشّحة) بدل `SLOT_TIMES_ET` الثابتة. `_scheduled_slots` يختار
   `VIDEOS_PER_DAY` نوافذ يوميًا عبر epsilon-greedy bandit
   (`stats_math.weighted_sample_without_replacement`) حسب `composite_score`
   التاريخي المسجَّل فعليًا لكل نافذة (عمود `slot_bucket` في `Stats`)، مع نسبة
   استكشاف عشوائي دائمة (`SLOT_EXPLORATION_EPSILON` = 20%) حتى لا يتجمّد على
   أول نتيجة جيدة بالصدفة.

5. **مقياس نجاح أفضل: composite_score بدل views الخام.** `stats_math.composite_score()`
   يدمج: نسبة الاحتفاظ الحقيقية (`avg_view_percentage_72h` عبر Analytics API -
   المكوّن المهيمن 55% إن توفّرت)، نسبة تفاعل مرجّحة (likes + comments×2)/views،
   ولوغاريتم views كمكوّن صغير فقط (لتفادي هيمنة فيديو "فيرال بالصدفة" على كل
   القرار). **retention يحتاج إعدادًا إضافيًا لمرة واحدة - راجع القسم أدناه.**

6. **A/B على الزاوية/الهوك (نسخة مخفّفة، صادقة بحدودها).** لا يوجد A/B حقيقي
   على نفس الفيديو (نشر عنوانين لفيديو واحد غير عملي هنا)، لكن كل موضوع يُوسَم
   بـ`angle`، ومتوسط الأداء لكل زاوية يُغذّى مجددًا لجمناي عند اختيار زوايا
   المواضيع القادمة (نقطة 3 أعلاه) - تعلّم على مستوى "نمط الهوك عبر مواضيع
   مختلفة" وليس اختبار A/B صارم على نفس الفيديو.

## إعداد لمرة واحدة: تفعيل retention الحقيقي (الأهم)

النشر الحالي يستخدم `YOUTUBE_OAUTH_REFRESH_TOKEN` بصلاحية `youtube.upload` فقط،
وهذه لا تكفي لقراءة Analytics. الخطوات:

1. نفس OAuth Client الحالي في Google Cloud Console (لا حاجة لعميل جديد).
2. أعد عمل موافقة OAuth (consent flow) مرة واحدة، لكن اطلب هذه المرة **الصلاحيتين
   معًا** في نفس الطلب:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/yt-analytics.readonly`
3. ستحصل على `refresh_token` جديد يحمل الصلاحيتين. ضَع قيمته في سرّ GitHub
   Actions الجديد:
   ```
   YOUTUBE_ANALYTICS_REFRESH_TOKEN
   ```
   (اسم متغيّر البيئة بالضبط - راجع `planner/analytics_client.py` و`1.env.example`).
4. بدون هذا السر، النظام يعمل تمامًا كما هو (views/likes/comments فقط) دون أي
   كسر - `analytics_client.is_configured()` تتحقق من وجوده أولًا في كل استدعاء.

## بنية التبويبات (بعد الترحيل التلقائي)

**Config**:
`week_number | active_categories | locked | last_updated_utc | tested_history | last_reevaluation_utc | challenger_rotation_index | swap_log`

**Plan**:
`row_id | category | topic | scheduled_date | scheduled_time_et | scheduled_datetime_utc | status | video_id | notes | slot_bucket | is_exploration | angle`

**Stats**:
`video_id | category | topic | title | script | published_at_utc | check_24h_due_utc | views_24h | likes_24h | comments_24h | check_48h_due_utc | views_48h | likes_48h | comments_48h | check_72h_due_utc | views_72h | likes_72h | comments_72h | stats_complete | avg_view_percentage_72h | avg_view_duration_sec_72h | slot_bucket | is_exploration | angle`

> **ملاحظة ترحيل:** لو كان لديك شيت من نسخة سابقة، `sheets_client.ensure_sheet_structure()`
> يكتشف الأعمدة الناقصة تلقائيًا ويُلحقها في نهاية صف العناوين دون تغيير ترتيب
> الأعمدة القديمة - بياناتك القديمة تبقى سليمة، والأعمدة الجديدة تبدأ فارغة.

## دورة الحياة

1. **أسابيع الاختبار (قبل القفل):** 3 فئات/أسبوع بالتدوير على كل الـ9 فئات
   (`TEST_WAVES`)، تتكرر الدورة تلقائيًا (لا تتوقف بعد جولة واحدة) حتى تتحقق
   الدلالة الإحصائية أو سقف الأمان (12 أسبوعًا).
2. **القفل:** عند تحقق الشرط، تُقفل أفضل 3 فئات (`Config.locked = TRUE`).
3. **بعد القفل:** خطة شهرية (~60 فيديو) بـ90% فئات مقفلة + 10% فئة تجريبية
   دوّارة، وإعادة تقييم دورية كل 30 يومًا قد تستبدل أضعف فئة مقفلة تلقائيًا.

## ملاحظات مهمة (باقية من التصميم الأصلي)

- **الحالة تُخزَّن بالكامل في الشيت نفسه (تبويب Config)** وليس في ملف محلي، لأن
  عمّال GitHub Actions لا يحتفظون بأي قرص بين التشغيلات.
- التوقيت بتوقيت `America/New_York` ويُحوَّل تلقائيًا لتوقيت UTC الصحيح حسب DST
  عبر `zoneinfo`.
