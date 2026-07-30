"""
المنسّق الرئيسي لخط الأنابيب الكامل (المراحل 1-9).

الاستخدام:
    python main.py --category "علوم وحقائق مذهلة"

يتطلب ملف .env (انظر .env.example) يحوي كل الأسرار المذكورة.
"""

import json
import os
import sys
import argparse
import logging
import time

from dotenv import load_dotenv
from moviepy.editor import AudioFileClip

load_dotenv()

# ملاحظة مهمة عن التسجيل:
# logging.basicConfig() الافتراضي يفتح StreamHandler على sys.stderr، بينما
# moviepy (write_videofile) يطبع شريط التقدّم على sys.stdout. لو التُقط
# الناتج عبر تيار واحد فقط (مثلاً `python main.py > log.txt` يلتقط stdout
# فقط)، تختفي رسائل logger.warning/info من الملف تمامًا رغم أنها نُفّذت
# فعليًا، فيبدو الأمر وكأن الرندر بدأ دون أي سبب مسجَّل قبله. لتفادي هذا
# نجبر كل السجلات على الذهاب لنفس تيار stdout الذي يستخدمه moviepy، مع
# flush فوري لكل سطر حتى يبقى الترتيب الزمني صحيحًا حتى عند التقاط الناتج
# بأدوات لا تفصل بين stdout/stderr بشكل موثوق.
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.flush = sys.stdout.flush  # تأكيد الـ flush الفوري
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_stdout_handler],
    force=True,
)
logger = logging.getLogger("main")


def _flush_logs():
    for h in logging.getLogger().handlers:
        h.flush()


from shared.state import RunState
from modules import (
    topic_selector,
    fact_collector,
    script_and_seo_planner,
    ai_media_verification,
    voice_generator,
    subtitle_generator,
    video_composer,
    final_scene_audit,
    competitor_seo_optimizer,
    publisher,
)
from config import MAX_SCENE_AUDIT_RETRIES


def _notify_manual_review(topic: str, category: str, message: str, video_id: str | None = None) -> None:
    """
    إشعار خارجي (Telegram) عند الحاجة لمراجعة يدوية، حتى لا يبقى التنبيه مجرد
    سطر في اللوغ المحلي الذي لا يُراقَب باستمرار (خصوصًا عند التشغيل التلقائي
    عبر weekly_planner/run_from_plan). لا يفشل الخط الرئيسي لو الإشعار نفسه
    فشل أو لم تُضبط أسرار Telegram - يبقى best-effort فقط.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID غير مضبوطين، تعذّر إرسال إشعار "
            "مراجعة يدوية خارج اللوغ المحلي."
        )
        return
    text = f"⚠️ مراجعة يدوية مطلوبة\nالفئة: {category}\nالموضوع: {topic}\n{message}"
    if video_id:
        text += f"\nvideo_id: {video_id}"
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("فشل إرسال إشعار Telegram للمراجعة اليدوية: %s", e)


def run_pipeline(category: str, run_dir: str = None, dry_run_publish: bool = False,
                  privacy_status: str = "public", fixed_topic: str | None = None):
    """
    fixed_topic: إن مُرِّر (من planner/run_from_plan.py عند اختيار الموضوع من خطة
    Google Sheets مسبقًا)، تُتخطى المرحلة 1 (topic_selector) تمامًا ويُستخدم هذا
    الموضوع كما هو. لا تأثير على أي مرحلة أخرى من مراحل الرندر/التحقق/التدقيق.
    """
    run_dir = run_dir or os.path.join("runs", time.strftime("%Y%m%d_%H%M%S"))
    media_dir = os.path.join(run_dir, "media")
    os.makedirs(run_dir, exist_ok=True)
    state = RunState(run_dir)

    # 1) اختيار الموضوع
    if fixed_topic:
        topic = fixed_topic
        logger.info("=== المرحلة 1: تم تمرير موضوع محدد مسبقًا من الخطة، تخطي جمناي: %s ===", topic)
    else:
        logger.info("=== المرحلة 1: اختيار الموضوع ===")
        topic = topic_selector.select_topic(category)
        logger.info("الموضوع المختار: %s", topic)

    # 2) جمع الحقائق
    logger.info("=== المرحلة 2: جمع الحقائق ===")
    facts = fact_collector.collect_facts(topic)

    # 3) السكربت + خطة المشاهد + SEO أولي
    logger.info("=== المرحلة 3: السكربت وخطة المشاهد ===")
    plan = script_and_seo_planner.build_plan(topic, facts)
    state.data["narration"] = plan["narration"]
    state.data["video_title"] = plan["video_title"]
    state.data["youtube_keywords"] = plan["youtube_keywords"]
    state.save()
    # تسجيل ترتيب المشاهد كما ورد في السكربت، لتمكين مقارنة كل مشهد بصريًا مع
    # المشهد السابق مباشرة له أثناء التحقق (ai_media_verification).
    state.set_scene_order([s["id"] for s in plan["scenes"]])
    # نثبّت عقد كل مشهد (النص + الكيان المطلوب actor_ref + الكلمات المفتاحية)
    # مرة واحدة هنا، قبل أي تحقق بصري، حتى يبقى "المطلوب" ثابتًا ومنفصلًا عن
    # أي تحليل بصري لاحق طوال بقية التشغيل.
    for s in plan["scenes"]:
        state.set_scene_contract(s["id"], {
            "text": s.get("text"),
            "actor_ref": s.get("actor_ref"),
            "visual_keywords": s.get("visual_keywords", []),
        })

    # 4+5) البحث المتوازي عن الوسائط + التحقق البصري لكل مشهد
    logger.info("=== المراحل 4-5: البحث والتحقق من الوسائط لكل مشهد ===")
    scene_results = []
    for scene in plan["scenes"]:
        result = ai_media_verification.verify_scene_media(scene, state, media_dir, category=category, topic=topic)
        if result is None:
            raise RuntimeError(f"فشل نهائي في إيجاد وسائط مناسبة للمشهد {scene['id']}")
        rewritten = result.get("rewritten_narration_text")
        if rewritten:
            original_text = result.get("original_narration_text", scene["text"])
            if original_text in plan["narration"]:
                plan["narration"] = plan["narration"].replace(original_text, rewritten, 1)
            else:
                logger.warning(
                    "[%s] تعذّر إيجاد النص الأصلي حرفيًا داخل narration الكامل لاستبداله؛ "
                    "narration الكامل لن يعكس النص المعاد صياغته لهذا المشهد.",
                    scene["id"],
                )
            scene["text"] = rewritten
            logger.info("[%s] استُبدل نص السرد بنسخة معاد صياغتها لتناسب الصور المتاحة.", scene["id"])
        scene_results.append(result)

    # الصوت والترجمة
    logger.info("=== توليد الصوت والترجمة ===")
    audio_path = os.path.join(run_dir, "narration.mp3")
    word_boundaries = voice_generator.generate_voice(plan["narration"], audio_path)
    subtitle_segments = subtitle_generator.build_subtitle_segments(word_boundaries)
    subtitle_generator.write_srt(subtitle_segments, os.path.join(run_dir, "subtitles.srt"))

    # نحسب زمن نطق كل مشهد فعليًا من الصوت (بدل الاعتماد فقط على best_segment
    # الخام من المرحلة 5)، لضمان تزامن حقيقي بين الصورة والصوت. لو edge-tts
    # لم يرجّع word boundaries، نمرّر مدة الصوت الإجمالية كاحتياط للتوزيع
    # النسبي بدل فقدان التزامن بصمت.
    total_audio_duration = AudioFileClip(audio_path).duration
    scene_timings = voice_generator.compute_scene_timings(
        plan["scenes"], word_boundaries, total_duration=total_audio_duration
    )
    for scene, result in zip(plan["scenes"], scene_results):
        timing = scene_timings.get(scene["id"])
        if timing:
            start_t, end_t = timing
            result["audio_duration"] = max(end_t - start_t, 0.1)

    # 6) المونتاج والتصدير (الرندر الأول والوحيد لحد الآن)
    logger.info("=== المرحلة 6: المونتاج والتصدير ===")
    _flush_logs()
    final_video_path = os.path.join(run_dir, "final_video.mp4")
    # مجلد كاش مقاطع المشاهد: يخزَّن كل مشهد كملف مُصيَّر مسبقًا، بحيث تُعاد
    # مشاهد الجولات اللاحقة من هذا الكاش بدل إعادة معالجتها من مصدرها الخام
    # في كل مرة يُستبدل فيها مشهد واحد فقط بعد فشل التدقيق (المرحلة 7).
    scene_cache_dir = os.path.join(run_dir, "scene_clips_cache")
    video_composer.compose_video(
        scene_results, audio_path, subtitle_segments, final_video_path,
        scene_cache_dir=scene_cache_dir,
    )

    # 7) التدقيق النهائي لكل مشهد (حلقة إعادة بناء المشاهد المرفوضة)
    #
    # تصحيح مهم: سابقًا كان الكود يستدعي رندر الفيديو الكامل مرة واحدة لكل
    # مشهد مرفوض على حدة (داخل حلقة for f in failed)، فإن رفض التدقيق 3
    # مشاهد في نفس الجولة كانت النتيجة 3 عمليات رندر كاملة متتالية. الآن:
    # نجمع كل استبدالات المشاهد المرفوضة أولاً (بلا أي رندر)، ثم نعيد الرندر
    # مرة واحدة فقط في نهاية الجولة، وفقط إن استُبدل مشهد واحد على الأقل
    # فعليًا. إن فشلت كل محاولات الاستبدال في هذه الجولة، لا يحدث أي رندر.
    logger.info("=== المرحلة 7: التدقيق النهائي ===")
    manual_review_required = False
    for attempt in range(MAX_SCENE_AUDIT_RETRIES):
        _flush_logs()
        audit_results = final_scene_audit.audit_video(final_video_path, plan["narration"], plan["scenes"])
        failed = final_scene_audit.get_failed_scenes(audit_results)
        if not failed:
            logger.info("كل المشاهد اجتازت التدقيق.")
            break

        logger.warning("مشاهد مرفوضة (%d): %s", len(failed), [f["scene_id"] for f in failed])
        _flush_logs()

        any_replaced = False
        for f in failed:
            scene_index = next((i for i, s in enumerate(plan["scenes"]) if s["id"] == f["scene_id"]), None)
            if scene_index is None:
                logger.warning("scene_id غير معروف من التدقيق: %s، تجاهل.", f["scene_id"])
                continue
            scene = plan["scenes"][scene_index]
            alt_keywords = final_scene_audit.request_alternative_keywords_for_scene(
                plan["narration"], scene["text"], f.get("issue", "")
            )
            scene["visual_keywords"] = alt_keywords
            new_result = ai_media_verification.verify_scene_media(scene, state, media_dir, category=category, topic=topic)
            if new_result is None:
                logger.error("تعذر إيجاد بديل للمشهد %s، سيبقى كما هو.", f["scene_id"])
                continue
            timing = scene_timings.get(scene["id"])
            if timing:
                start_t, end_t = timing
                new_result["audio_duration"] = max(end_t - start_t, 0.1)
            scene_results[scene_index] = new_result
            any_replaced = True

        # رندر واحد فقط بعد تجميع كل استبدالات هذه الجولة، وليس رندرًا
        # منفصلًا لكل مشهد مرفوض.
        if any_replaced:
            logger.info("إعادة رندر الفيديو مرة واحدة بعد استبدال %d مشهد/مشاهد في هذه الجولة "
                        "(المشاهد غير المتغيّرة تُقرأ من الكاش، فقط المُستبدَلة تُعاد معالجتها).",
                        sum(1 for _ in failed))
            _flush_logs()
            video_composer.compose_video(
                scene_results, audio_path, subtitle_segments, final_video_path,
                scene_cache_dir=scene_cache_dir,
            )
        else:
            logger.warning("لم يُستبدل أي مشهد فعليًا في هذه الجولة، تخطي إعادة الرندر.")
    else:
        logger.warning("تم الوصول للحد الأقصى من محاولات التدقيق (%d) مع بقاء مشاهد دون المستوى.",
                        MAX_SCENE_AUDIT_RETRIES)
        manual_review_required = True

    # نقطة 3: أي مشهد استخدم fallback بدرجة أقل من الحد الأدنى المطلق (
    # MIN_FALLBACK_ACCEPT_SCORE) يُعلَّم needs_manual_review من داخل
    # ai_media_verification، فنجمعها هنا أيضًا لتوحيد قرار النشر.
    if any(r.get("needs_manual_review") for r in scene_results):
        manual_review_required = True

    # ملف تجميعي واحد يحوي التحليل البصري الكامل (visual_summary) لكل مشهد
    # تم قبوله فعليًا فقط، بترتيب ظهورها في الفيديو، لمراجعة اتساق الفيديو
    # الكلي لاحقًا (يدويًا أو آليًا) دون الحاجة لإعادة فتح كل مشهد على حدة.
    accepted_visual_analysis = []
    for scene in plan["scenes"]:
        scene_id = scene["id"]
        result = state.data["scenes"].get(scene_id, {})
        accepted_visual_analysis.append({
            "scene_id": scene_id,
            "text": scene["text"],
            "score": result.get("score"),
            "media_type": result.get("media_type"),
            "needs_manual_review": result.get("needs_manual_review", False),
            "visual_summary": state.data.get("scene_visual_summaries", {}).get(scene_id),
        })
    with open(os.path.join(run_dir, "accepted_scenes_visual_analysis.json"), "w", encoding="utf-8") as f:
        json.dump(accepted_visual_analysis, f, ensure_ascii=False, indent=2)

    # 8) تحسين SEO بمقارنة المنافسين
    logger.info("=== المرحلة 8: تحسين السيو ===")
    competitor_videos = competitor_seo_optimizer.fetch_competitor_videos(plan["youtube_keywords"])
    seo = competitor_seo_optimizer.optimize_seo(plan["video_title"], plan["narration"], competitor_videos)

    # نضيف أسطر المصادر الإلزامية (نقطة 5/6/7) في نهاية الوصف فقط للمقاطع
    # التي تتطلب فعليًا ذكر المصدر حسب رخصتها (CC-BY / CC-BY-SA)، بينما
    # المقاطع من CC0/Public Domain (بما فيها أغلب محتوى ناسا) لا تُضاف هنا
    # لأنها لا تتطلب إسنادًا قانونيًا.
    attributions = [
        r["attribution_text"] for r in scene_results
        if r.get("requires_attribution") and r.get("attribution_text")
    ]
    if attributions:
        sources_block = "\n\nSources:\n" + "\n".join(f"- {a}" for a in dict.fromkeys(attributions))
        seo["optimized_description"] = (seo["optimized_description"] + sources_block)[:5000]

    # 9) النشر
    logger.info("=== المرحلة 9: النشر ===")
    common_fields = {"topic": topic, "category": category, "narration": plan["narration"]}

    if dry_run_publish:
        logger.info("dry_run_publish=True، تخطي النشر الفعلي. النتيجة النهائية جاهزة في: %s", final_video_path)
        return {"video_path": final_video_path, "seo": seo, **common_fields}

    # نقطة 1: لو بقيت مشاهد دون العتبة (حلقة التدقيق خرجت عبر else) أو أي
    # مشهد استُخدم فيه fallback أقل من الحد الأدنى المطلق، نتوقف قبل النشر
    # التلقائي العادي، ونحفظ الفيديو كمسودة unlisted/private (الأكثر تحفظًا:
    # private) مع رسالة واضحة تطلب المراجعة اليدوية، بدل المتابعة التلقائية.
    if manual_review_required:
        forced_privacy = "private"
        logger.warning(
            "⚠️ الفيديو يحتاج مراجعة يدوية: بقيت مشاهد دون عتبة الجودة المطلوبة "
            "(تدقيق نهائي و/أو fallback وسائط دون الحد الأدنى). تم حفظ الفيديو "
            "كمسودة بحالة خصوصية '%s' بدل نشره تلقائيًا كما هو معتاد. الرجاء "
            "مراجعة الفيديو يدويًا قبل تغيير حالة الخصوصية إلى عام.",
            forced_privacy,
        )
        video_id = publisher.publish_video(
            video_path=final_video_path,
            title=seo["optimized_title"],
            description=seo["optimized_description"],
            tags=seo["tags"],
            privacy_status=forced_privacy,
        )
        _notify_manual_review(
            topic=topic, category=category,
            message=f"تم حفظ الفيديو كمسودة (خصوصية {forced_privacy}) بسبب مشاهد دون العتبة المطلوبة.",
            video_id=video_id,
        )
        return {
            "video_path": final_video_path, "video_id": video_id, "seo": seo,
            "manual_review_required": True, "privacy_status": forced_privacy,
            "message": "الفيديو نُشر كمسودة (خصوصية private) وينتظر مراجعتك اليدوية بسبب مشاهد دون العتبة المطلوبة.",
            **common_fields,
        }

    video_id = publisher.publish_video(
        video_path=final_video_path,
        title=seo["optimized_title"],
        description=seo["optimized_description"],
        tags=seo["tags"],
        privacy_status=privacy_status,
    )
    return {"video_path": final_video_path, "video_id": video_id, "seo": seo, **common_fields}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="خط أنابيب توليد ونشر فيديوهات يوتيوب شورتس")
    parser.add_argument("--category", required=True, help="الفئة العامة لاختيار الموضوع")
    parser.add_argument("--dry-run", action="store_true", help="تنفيذ كل المراحل عدا النشر الفعلي")
    parser.add_argument(
        "--privacy", choices=["public", "unlisted", "private"], default=None,
        help="حالة الخصوصية عند النشر. إن لم تُحدد، سيُسأل عنها تفاعليًا قبل البدء.",
    )
    args = parser.parse_args()

    privacy_status = args.privacy
    if privacy_status is None and not args.dry_run:
        choice = input("انشر الفيديو كـ (1) عام public  (2) غير مدرج unlisted  (3) خاص private؟ [1/2/3]: ").strip()
        privacy_status = {"1": "public", "2": "unlisted", "3": "private"}.get(choice, "public")
        logger.info("تم اختيار حالة الخصوصية: %s", privacy_status)
    elif privacy_status is None:
        privacy_status = "public"

    result = run_pipeline(args.category, dry_run_publish=args.dry_run, privacy_status=privacy_status)
    logger.info("النتيجة النهائية: %s", result)
