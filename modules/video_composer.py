"""
المرحلة 6: المونتاج النهائي.
- يقص كل فيديو حسب best_segment العائد من المرحلة 5 (وليس أول 10 ثوانٍ فقط).
- انتقالات "fade" سريعة بين المشاهد.
- يدمج الصوت (narration) والترجمة (SRT كاملة).
- يصدّر بدقة 1080x1920 (عمودي، مناسب لليوتيوب شورتس).

يدعم أيضًا استبدال مشهد واحد فقط وإعادة الرندر الجزئي (مطلوب في المرحلة 7).
"""

import logging

from moviepy.editor import (
    VideoFileClip, CompositeVideoClip, concatenate_videoclips,
    AudioFileClip, TextClip, CompositeAudioClip,
)

from config import EXPORT_RESOLUTION, FADE_DURATION

logger = logging.getLogger("modules.video_composer")


def _prepare_clip(scene_result: dict, target_size: tuple[int, int]):
    clip = VideoFileClip(scene_result["clip_path"]).subclip(
        scene_result["start"], scene_result["end"]
    )
    # يملأ الإطار العمودي (crop-to-fill) بدل تشويه الأبعاد
    clip = clip.resize(height=target_size[1])
    if clip.w < target_size[0]:
        clip = clip.resize(width=target_size[0])
    clip = clip.crop(
        x_center=clip.w / 2, y_center=clip.h / 2,
        width=target_size[0], height=target_size[1],
    )
    return clip.fadein(FADE_DURATION).fadeout(FADE_DURATION)


def compose_video(scene_results: list[dict], narration_audio_path: str, subtitle_segments: list[dict],
                   out_path: str, size: tuple[int, int] = EXPORT_RESOLUTION):
    clips = [_prepare_clip(sr, size) for sr in scene_results]
    video = concatenate_videoclips(clips, method="compose", padding=-FADE_DURATION)

    narration = AudioFileClip(narration_audio_path)
    video = video.set_audio(narration).set_duration(narration.duration)

    subtitle_clips = []
    for seg in subtitle_segments:
        txt = (
            TextClip(seg["text"], fontsize=60, color="white", font="Arial",
                     stroke_color="black", stroke_width=2, method="caption",
                     size=(int(size[0] * 0.9), None))
            .set_start(seg["start"])
            .set_end(seg["end"])
            .set_position(("center", "bottom"))
        )
        subtitle_clips.append(txt)

    final = CompositeVideoClip([video] + subtitle_clips, size=size)
    final.write_videofile(
        out_path, fps=30, codec="libx264", audio_codec="aac",
        preset="medium", threads=4,
    )
    for c in clips:
        c.close()
    final.close()
    return out_path


def replace_scene_and_rerender(scene_results: list[dict], scene_index: int, new_scene_result: dict,
                                narration_audio_path: str, subtitle_segments: list[dict], out_path: str,
                                size: tuple[int, int] = EXPORT_RESOLUTION):
    """يستبدل مشهدًا واحدًا فقط في القائمة ثم يعيد الرندر الكامل (moviepy لا يدعم رندرًا جزئيًا حقيقيًا،
    لكن الاستبدال هنا منطقي: فقط ملف المصدر للمشهد المرفوض يتغيّر، والبقية تبقى كما هي)."""
    updated = list(scene_results)
    updated[scene_index] = new_scene_result
    return compose_video(updated, narration_audio_path, subtitle_segments, out_path, size)
