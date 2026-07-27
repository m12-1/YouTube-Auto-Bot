"""
المنسّق الرئيسي لخط الأنابيب الكامل (المراحل 1-9).

الاستخدام:
    python main.py --category "علوم وحقائق مذهلة"

يتطلب ملف .env (انظر .env.example) يحوي كل الأسرار المذكورة.
"""

import os
import argparse
import logging
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")

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


def run_pipeline(category: str, run_dir: str = None, dry_run_publish: bool = False,
                  privacy_status: str = "public"):
    run_dir = run_dir or os.path.join("runs", time.strftime("%Y%m%d_%H%M%S"))
    media_dir = os.path.join(run_dir, "media")
    os.makedirs(run_dir, exist_ok=True)
    state = RunState(run_dir)

    # 1) اختيار الموضوع
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

    # 4+5) البحث المتوازي عن الوسائط + التحقق البصري لكل مشهد
    logger.info("=== المراحل 4-5: البحث والتحقق من الوسائط لكل مشهد ===")
    scene_results = []
    for scene in plan["scenes"]:
        result = ai_media_verification.verify_scene_media(scene, state, media_dir)
        if result is None:
            raise RuntimeError(f"فشل نهائي في إيجاد وسائط مناسبة للمشهد {scene['id']}")
        scene_results.append(result)

    # الصوت والترجمة
    logger.info("=== توليد الصوت والترجمة ===")
    audio_path = os.path.join(run_dir, "narration.mp3")
    word_boundaries = voice_generator.generate_voice(plan["narration"], audio_path)
    subtitle_segments = subtitle_generator.build_subtitle_segments(word_boundaries)
    subtitle_generator.write_srt(subtitle_segments, os.path.join(run_dir, "subtitles.srt"))

    # نحسب زمن نطق كل مشهد فعليًا من الصوت (بدل الاعتماد فقط على best_segment
    # الخام من المرحلة 5)، لضمان تزامن حقيقي بين الصورة والصوت.
    scene_timings = voice_generator.compute_scene_timings(plan["scenes"], word_boundaries)
    for scene, result in zip(plan["scenes"], scene_results):
        timing = scene_timings.get(scene["id"])
        if timing:
            start_t, end_t = timing
            result["audio_duration"] = max(end_t - start_t, 0.1)

    # 6) المونتاج والتصدير
    logger.info("=== المرحلة 6: المونتاج والتصدير ===")
    final_video_path = os.path.join(run_dir, "final_video.mp4")
    video_composer.compose_video(scene_results, audio_path, subtitle_segments, final_video_path)

    # 7) التدقيق النهائي لكل مشهد (حلقة إعادة بناء المشاهد المرفوضة)
    logger.info("=== المرحلة 7: التدقيق النهائي ===")
    for attempt in range(MAX_SCENE_AUDIT_RETRIES):
        audit_results = final_scene_audit.audit_video(final_video_path, plan["narration"], plan["scenes"])
        failed = final_scene_audit.get_failed_scenes(audit_results)
        if not failed:
            logger.info("كل المشاهد اجتازت التدقيق.")
            break

        logger.warning("مشاهد مرفوضة (%d): %s", len(failed), [f["scene_id"] for f in failed])
        for f in failed:
            scene_index = next(i for i, s in enumerate(plan["scenes"]) if s["id"] == f["scene_id"])
            scene = plan["scenes"][scene_index]
            alt_keywords = final_scene_audit.request_alternative_keywords_for_scene(
                plan["narration"], scene["text"], f.get("issue", "")
            )
            scene["visual_keywords"] = alt_keywords
            new_result = ai_media_verification.verify_scene_media(scene, state, media_dir)
            if new_result is None:
                logger.error("تعذر إيجاد بديل للمشهد %s، سيبقى كما هو.", f["scene_id"])
                continue
            timing = scene_timings.get(scene["id"])
            if timing:
                start_t, end_t = timing
                new_result["audio_duration"] = max(end_t - start_t, 0.1)
            scene_results[scene_index] = new_result
            video_composer.replace_scene_and_rerender(
                scene_results, scene_index, new_result, audio_path, subtitle_segments, final_video_path
            )
    else:
        logger.warning("تم الوصول للحد الأقصى من محاولات التدقيق (%d) مع بقاء مشاهد دون المستوى.",
                        MAX_SCENE_AUDIT_RETRIES)

    # 8) تحسين SEO بمقارنة المنافسين
    logger.info("=== المرحلة 8: تحسين السيو ===")
    competitor_videos = competitor_seo_optimizer.fetch_competitor_videos(plan["youtube_keywords"])
    seo = competitor_seo_optimizer.optimize_seo(plan["video_title"], plan["narration"], competitor_videos)

    # 9) النشر
    logger.info("=== المرحلة 9: النشر ===")
    if dry_run_publish:
        logger.info("dry_run_publish=True، تخطي النشر الفعلي. النتيجة النهائية جاهزة في: %s", final_video_path)
        return {"video_path": final_video_path, "seo": seo}

    video_id = publisher.publish_video(
        video_path=final_video_path,
        title=seo["optimized_title"],
        description=seo["optimized_description"],
        tags=seo["tags"],
        privacy_status=privacy_status,
    )
    return {"video_path": final_video_path, "video_id": video_id, "seo": seo}


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
