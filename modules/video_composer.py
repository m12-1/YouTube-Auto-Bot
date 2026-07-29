"""
المرحلة 6: المونتاج النهائي.
- يقص كل فيديو حسب best_segment العائد من المرحلة 5 (وليس أول 10 ثوانٍ فقط).
- انتقالات "crossfade" (تلاشي متبادل حقيقي) سريعة بين المشاهد.
- يدمج الصوت (narration) والترجمة (SRT كاملة).
- يصدّر بدقة 1080x1920 (عمودي، مناسب لليوتيوب شورتس).

يدعم أيضًا استبدال مشهد واحد فقط وإعادة الرندر الجزئي (مطلوب في المرحلة 7).
"""

import hashlib
import json
import logging
import os
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

from config import (
    EXPORT_RESOLUTION, FADE_DURATION,
    MAX_CLIP_SLOWDOWN_STRETCH_RATIO, MIN_SOURCE_SECONDS_FOR_BOOMERANG,
)

logger = logging.getLogger("modules.video_composer")

# أقصى عدد أجزاء (أمام/عكس بالتناوب) نبنيها في تمديد البومرانج. لقطة مصدر
# قصيرة جدًا مقابل فجوة كبيرة قد تحتاج نظريًا عشرات التكرارات لتغطية المدة
# المطلوبة، وهذا بالضبط نوع "تكرار المشهد" المرئي غير المرغوب حتى لو كان
# داخل مشهد واحد فقط. لذلك نحدّ العدد، وأي فجوة متبقية بعد الحد نغطيها
# بإبطاء التسلسل كاملاً بدل تكراره أكثر (انظر _extend_clip_to_duration).
_MAX_BOOMERANG_SEGMENTS = 4


_SUBTITLE_FONT_NAME = "DejaVu-Sans-Bold"
_SUBTITLE_FONT_SIZE = 60
_SUBTITLE_MAX_WIDTH_PX = int(EXPORT_RESOLUTION[0] * 0.9)


def _shape_arabic(text: str) -> str:
    """يهيّئ النص العربي (ربط الحروف وترتيب RTL) قبل تمريره لـ TextClip،
    لأن ImageMagick/PIL لا يقومان بذلك تلقائيًا. لا تأثير لهذه الدالة على
    نص إنجليزي بحت (يُعاد كما هو) — الفحص يتم عبر _contains_arabic حتى لا
    تُهدَر معالجة غير ضرورية على محتوى غير عربي."""
    if not _contains_arabic(text):
        return text
    return get_display(arabic_reshaper.reshape(text))


def _contains_arabic(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06FF" for ch in text)


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


def _extend_clip_to_duration(clip, target_duration: float, source_path: str, seg_start: float, seg_end: float):
    """
    يمدّد مقطعًا أقصر من زمن النطق المطلوب (audio_duration) لهذا المشهد،
    بدل تجميد الشاشة على إطار ثابت (vfx.freeze) الذي كان يُنتج "توقف
    الشاشة" الملحوظ عند بعض الانتقالات - وهو بالضبط ما لاحظته في الفيديو.

    الاستراتيجية مرتّبة حسب الأفضلية البصرية:
      1) إن كانت الفجوة صغيرة (النسبة <= MAX_CLIP_SLOWDOWN_STRETCH_RATIO):
         إبطاء بسيط لسرعة التشغيل. غير ملحوظ للعين في لقطات B-roll القصيرة،
         ولا يفقد أي حركة/محتوى من المقطع.
      2) فجوة أكبر: تمديد "بومرانج" (تشغيل للأمام ثم عكسي بالتناوب) بدل
         التجميد أو التكرار الحرفي - لا يوجد قطع مفاجئ لأن آخر إطار من كل
         جزء يطابق تمامًا أول إطار من الجزء التالي (نفس الإطار معكوسًا).
      3) فقط لو كان المصدر قصيرًا جدًا (< MIN_SOURCE_SECONDS_FOR_BOOMERANG)
         بحيث لا يمكن بناء بومرانج مفيد منه، نلجأ للتجميد كملاذ أخير نادر.

    ملاحظة مهمة (تم اكتشافها فعليًا أثناء اختبار الكود قبل التسليم، وليست
    افتراضية): بناء الاتجاه العكسي عبر vfx.time_mirror مباشرة على نفس كائن
    الفيديو المستخدم للاتجاه الأمامي يجعلهما يتشاركان نفس "قارئ" ffmpeg
    الداخلي (نفس الأنبوب/العملية الفرعية). قراءة نفس الأنبوب بترتيب عكسي
    تتطلب من moviepy إعادة تشغيل/البحث (seek) داخل نفس العملية الفرعية
    بشكل متكرر، مما يسبب أحيانًا فشل قراءة الإطار (IOError) عند الترميز
    الفعلي. لتفادي هذا نهائيًا، نفتح قارئًا مستقلاً تمامًا (VideoFileClip
    جديد من نفس ملف المصدر) خاصًا بالاتجاه العكسي فقط، فلا يتشارك أي حالة
    داخلية مع الاتجاه الأمامي.
    """
    if clip.duration <= 0.05:
        return clip.fx(vfx.freeze, t="end", total_duration=target_duration)

    stretch_ratio = target_duration / clip.duration
    if stretch_ratio <= MAX_CLIP_SLOWDOWN_STRETCH_RATIO:
        # factor < 1 يعني إبطاء (يمدّد المدة)؛ speedx(factor) ينتج مدة = الأصلية/factor.
        stretched = clip.fx(vfx.speedx, factor=1 / stretch_ratio)
        return stretched.set_duration(target_duration)

    if clip.duration < MIN_SOURCE_SECONDS_FOR_BOOMERANG:
        return clip.fx(vfx.freeze, t="end", total_duration=target_duration)

    forward = clip
    backward_source = VideoFileClip(source_path).subclip(seg_start, seg_end)
    backward = backward_source.fx(vfx.time_mirror)
    segments = []
    covered = 0.0
    flip = False
    # نحدّ عدد الأجزاء بـ _MAX_BOOMERANG_SEGMENTS: لقطة قصيرة جدًا مقابل فجوة
    # كبيرة قد تحتاج نظريًا عشرات الجولات أمام/عكس، وهذا يصبح تكرارًا مرئيًا
    # مزعجًا بحد ذاته حتى لو لم يكن "قطعًا" مفاجئًا. أي فجوة متبقية بعد الحد
    # تُغطى بإبطاء التسلسل كاملاً أدناه بدل تكراره أكثر.
    while covered < target_duration and len(segments) < _MAX_BOOMERANG_SEGMENTS:
        seg = backward if flip else forward
        segments.append(seg)
        covered += seg.duration
        flip = not flip
    # method="chain" يكفي هنا (لا padding، نفس الأبعاد تمامًا لأنها نفس
    # المصدر)، وأرخص من "compose" لأنه لا يحتاج تركيب طبقات.
    boomerang = concatenate_videoclips(segments, method="chain")

    if boomerang.duration < target_duration:
        # وصلنا للحد الأقصى للأجزاء قبل تغطية المدة المطلوبة كاملة (لقطة
        # قصيرة جدًا نسبيًا). نُبطئ التسلسل كاملاً ليمتد تمامًا لطول
        # target_duration بدل إضافة جولات بومرانج إضافية.
        slow_factor = boomerang.duration / target_duration  # < 1 => إبطاء
        boomerang = boomerang.fx(vfx.speedx, factor=slow_factor)

    return boomerang.subclip(0, target_duration)


def _prepare_clip(scene_result: dict, target_size: tuple[int, int],
                   is_first: bool = False, is_last: bool = False):
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
            # تصحيح: كانت هذه الاستدعاء تُمرِّر وسيطين فقط بينما الدالة تتطلب
            # source_path/seg_start/seg_end أيضًا (لازمة لفتح قارئ مستقل
            # للاتجاه العكسي في حالة البومرانج) — كانت ستفشل بـ TypeError عند
            # أول مشهد أقصر من زمن نطقه.
            clip = _extend_clip_to_duration(
                clip, target_duration,
                scene_result["clip_path"], scene_result["start"], scene_result["end"],
            )
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

    # انتقالات: المقطع الأول يتلاشى من الأسود عند بداية الفيديو (فتحة
    # لطيفة)، وكل مقطع تالٍ يستخدم crossfadein (تلاشٍ بالشفافية) بدل
    # fadein العادي (الذي يتلاشى من الأسود الصلب) - هذا هو الفرق الجوهري:
    # crossfadein يجعل بداية المقطع شفافة تدريجيًا فيظهر المقطع السابق من
    # خلفه أثناء التداخل الزمني الناتج عن padding سالب في concatenate_videoclips
    # أدناه، فينتج تلاشٍ متبادل حقيقي (dissolve) بين المشهدين بدل "قطع أسود"
    # قصير. المقطع الأخير يتلاشى للأسود عند نهاية الفيديو كخاتمة لطيفة.
    if is_first:
        clip = clip.fadein(FADE_DURATION)
    else:
        clip = clip.crossfadein(FADE_DURATION)
    if is_last:
        clip = clip.fadeout(FADE_DURATION)

    return clip


def _scene_cache_key(scene_result: dict, size: tuple[int, int],
                      is_first: bool, is_last: bool) -> str:
    """مفتاح يحدد بشكل فريد كل العوامل المؤثرة في شكل مقطع المشهد النهائي
    (المصدر، القص، مدة الصوت المطلوبة، الدقة، موقعه في التسلسل - أول/أخير
    مشهد يؤثران على نوع fade المُطبَّق). أي تغيّر في أي منها يُبطل الكاش
    لذلك المشهد فقط."""
    payload = {
        "clip_path": scene_result["clip_path"],
        "start": scene_result["start"],
        "end": scene_result["end"],
        "audio_duration": scene_result.get("audio_duration"),
        "size": size,
        "fade": FADE_DURATION,
        "is_first": is_first,
        "is_last": is_last,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _get_or_render_scene_clip(scene_id: str, scene_result: dict, size: tuple[int, int], cache_dir: str,
                               is_first: bool = False, is_last: bool = False):
    """يعيد مقطع الفيديو الجاهز (مع fade) لهذا المشهد: من الكاش إن كان
    موجودًا ومطابقًا (لا حاجة لإعادة قراءة/قص/تحجيم المصدر الخام)، أو
    يصيّره من جديد فقط إن تغيّر المصدر أو لم يكن مخزّنًا بعد.
    هذا هو ما يمنع إعادة معالجة كل المشاهد غير المرفوضة عند كل جولة تدقيق."""
    os.makedirs(cache_dir, exist_ok=True)
    key = _scene_cache_key(scene_result, size, is_first, is_last)
    cached_path = os.path.join(cache_dir, f"{scene_id}.mp4")
    meta_path = os.path.join(cache_dir, f"{scene_id}.key")

    if os.path.exists(cached_path) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                cached_key = f.read().strip()
        except Exception:
            cached_key = None
        if cached_key == key:
            logger.info("[%s] استخدام مقطع مُخزَّن مسبقًا (لم يتغيّر)، تخطي إعادة المعالجة.", scene_id)
            return VideoFileClip(cached_path)

    logger.info("[%s] تصيير مقطع جديد (مشهد جديد/مُستبدَل).", scene_id)
    fresh_clip = _prepare_clip(scene_result, size, is_first=is_first, is_last=is_last)
    fresh_clip.write_videofile(
        cached_path, fps=30, codec="libx264", audio=False,
        preset="medium", threads=4, logger=None,
    )
    fresh_clip.close()
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(key)
    return VideoFileClip(cached_path)


def compose_video(scene_results: list[dict], narration_audio_path: str, subtitle_segments: list[dict],
                   out_path: str, size: tuple[int, int] = EXPORT_RESOLUTION, scene_cache_dir: str | None = None):
    """يبني الفيديو النهائي. إن مُرِّر scene_cache_dir، تُعاد مقاطع المشاهد
    غير المتغيّرة من الكاش بدل إعادة معالجتها من مصدرها الخام في كل مرة —
    فقط المشاهد المُستبدَلة فعليًا (بعد فشل التدقيق) يُعاد تصييرها. تجميع
    المقاطع + الصوت + الترجمة في ملف واحد يبقى خطوة نهائية لا مفر منها
    (moviepy لا يدعم دمج صوت/ترجمة جزئيًا)، لكنها الآن خفيفة نسبيًا لأنها
    قراءة/دمج ملفات جاهزة الترميز بدل إعادة بناء كل مشهد من الصفر.
    """
    n = len(scene_results)
    if scene_cache_dir:
        # ملاحظة: fade/crossfade مطبّق بالفعل داخل _prepare_clip قبل الكتابة
        # إلى ملف الكاش، فلا يُعاد تطبيقه هنا (تجنّبًا لمضاعفة التأثير عند
        # القراءة من الكاش في المرات اللاحقة).
        clips = [
            _get_or_render_scene_clip(
                sr.get("scene_id") or sr.get("id") or f"scene_{i}",
                sr, size, scene_cache_dir,
                is_first=(i == 0), is_last=(i == n - 1),
            )
            for i, sr in enumerate(scene_results)
        ]
    else:
        clips = [
            _prepare_clip(sr, size, is_first=(i == 0), is_last=(i == n - 1))
            for i, sr in enumerate(scene_results)
        ]
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
