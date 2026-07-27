"""
المرحلة 6: المونتاج النهائي.
- يقص كل فيديو حسب best_segment العائد من المرحلة 5 (وليس أول 10 ثوانٍ فقط).
- انتقالات "fade" سريعة بين المشاهد.
- يدمج الصوت (narration) والترجمة (SRT كاملة).
- يصدّر بدقة 1080x1920 (عمودي، مناسب لليوتيوب شورتس).

يدعم أيضًا استبدال مشهد واحد فقط وإعادة الرندر الجزئي (مطلوب في المرحلة 7).
"""

import logging
import subprocess

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import ImageFont

# توافقية Pillow>=10 مع moviepy 1.0.3: الإصدارات الحديثة من Pillow أزالت
# PIL.Image.ANTIALIAS (كان يشير إلى LANCZOS)، بينما moviepy القديمة ما زالت
# تستخدمه داخليًا في fx/resize.py، فيفشل بـ AttributeError دون هذا الترقيع.
from PIL import Image as _PILImage
if not hasattr(_PILImage, "ANTIALIAS"):
    _PILImage.ANTIALIAS = _PILImage.Resampling.LANCZOS

from moviepy.editor import (
    VideoFileClip, CompositeVideoClip, concatenate_videoclips,
    AudioFileClip, TextClip, CompositeAudioClip, vfx,
)

from config import EXPORT_RESOLUTION, FADE_DURATION

logger = logging.getLogger("modules.video_composer")


_SUBTITLE_FONT_NAME = "Amiri-Bold"
_SUBTITLE_FONT_SIZE = 60
_SUBTITLE_MAX_WIDTH_PX = int(EXPORT_RESOLUTION[0] * 0.9)


def _shape_arabic(text: str) -> str:
    """يهيّئ النص العربي (ربط الحروف وترتيب RTL) قبل تمريره لـ TextClip،
    لأن ImageMagick/PIL لا يقومان بذلك تلقائيًا."""
    return get_display(arabic_reshaper.reshape(text))


def _resolve_font_path(font_name: str = _SUBTITLE_FONT_NAME) -> str | None:
    """يحل اسم خط ImageMagick (مثل Amiri-Bold) إلى مسار ملف .ttf فعلي عبر
    fc-match، لاستخدامه في قياس العرض الحقيقي بالبكسل عبر PIL. يرجع None
    لو تعذّر الحل (مثلاً fontconfig غير متوفر)."""
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", font_name],
            capture_output=True, text=True, timeout=5, check=True,
        )
        path = result.stdout.strip()
        return path or None
    except Exception:
        logger.warning("تعذّر حلّ مسار الخط %s عبر fc-match؛ سيُستخدم تقدير احتياطي بعدد الأحرف.", font_name)
        return None


def _wrap_arabic_by_pixel_width(
    text: str,
    max_width_px: int = _SUBTITLE_MAX_WIDTH_PX,
    font_size: int = _SUBTITLE_FONT_SIZE,
) -> str:
    """يقسّم نص الترجمة لأسطر بحيث يضمن كل سطر أنه لن يحتاج لفّ إضافي داخل
    TextClip، عبر قياس العرض الفعلي بالبكسل لكل سطر بعد تهيئته عربيًا
    (reshape + bidi) بواسطة PIL.ImageFont، بدل الاعتماد على عدد أحرف ثابت
    (_MAX_CHARS_PER_LINE سابقًا) الذي لا يعكس عرض الخط الفعلي. كل سطر
    يُشكَّل (يُعاد ترتيبه بصريًا) بمعزل عن الأسطر الأخرى، فلا يعاد لفّه
    لاحقًا بمنطق LTR عادي يكسر ترتيب الأحرف.

    نمرّر النتيجة لـ TextClip عبر method='label' الذي يحترم '\\n' حرفيًا
    ولا يعيد اللفّ، بعكس method='caption'.
    """
    words = text.split()
    if not words:
        return ""

    font_path = _resolve_font_path()
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            logger.warning("تعذّر تحميل الخط من %s عبر PIL؛ سيُستخدم تقدير احتياطي بعدد الأحرف.", font_path)
            font = None

    def shaped_width(candidate_words: list[str]) -> float:
        raw_line = " ".join(candidate_words)
        shaped = _shape_arabic(raw_line)
        if font is not None:
            return font.getlength(shaped)
        # تقدير احتياطي تقريبي لو تعذّر تحميل الخط: عدد أحرف بدل بكسل فعلي.
        return len(shaped) * (font_size * 0.55)

    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = current + [word]
        if current and shaped_width(candidate) > max_width_px:
            lines.append(_shape_arabic(" ".join(current)))
            current = [word]
        else:
            current = candidate
    if current:
        lines.append(_shape_arabic(" ".join(current)))

    return "\n".join(lines)


def _prepare_clip(scene_result: dict, target_size: tuple[int, int]):
    clip = VideoFileClip(scene_result["clip_path"]).subclip(
        scene_result["start"], scene_result["end"]
    )

    # إن توفر زمن نطق فعلي لهذا المشهد (من مطابقة الصوت الحقيقي عبر
    # voice_generator.compute_scene_timings)، نضبط مدة المقطع المرئي عليه
    # بدل تركه بطول best_segment الخام غير المرتبط بزمن النطق الفعلي.
    # هذا يمنع انزياح الصوت عن الصورة تدريجيًا مع تراكم المشاهد.
    target_duration = scene_result.get("audio_duration")
    if target_duration and target_duration > 0:
        if clip.duration < target_duration:
            # نمدّد اللقطة بتجميد آخر إطار (freeze-frame) بدل تكرارها (loop)،
            # لأن التكرار الحرفي يُنتج قطعًا مفاجئًا واضحًا للعين ويكسر
            # الإحساس بالاحترافية. التجميد على آخر فريم أكثر سلاسة وطبيعية.
            clip = clip.fx(vfx.freeze, t="end", total_duration=target_duration)
        elif clip.duration > target_duration:
            clip = clip.subclip(0, target_duration)

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
        wrapped_text = _wrap_arabic_by_pixel_width(
            seg["text"], max_width_px=int(size[0] * 0.9), font_size=_SUBTITLE_FONT_SIZE
        )
        txt = (
            # method='label' (بدل 'caption') يحترم أسطر '\n' كما هي دون إعادة
            # لفّها؛ اللفّ الفعلي محسوب مسبقًا بعرض بكسل حقيقي عبر
            # _wrap_arabic_by_pixel_width بدل الاعتماد على لفّ ImageMagick
            # التلقائي الذي يكسر ترتيب النص العربي المُهيَّأ بصريًا مسبقًا.
            TextClip(wrapped_text, fontsize=_SUBTITLE_FONT_SIZE, color="white",
                     font=_SUBTITLE_FONT_NAME,
                     stroke_color="black", stroke_width=2, method="label")
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
