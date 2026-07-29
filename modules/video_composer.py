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

# عدد خيوط ترميز ffmpeg: نستخدم أنوية المعالج المتاحة فعليًا بدل رقم 2 ثابت،
# لتسريع الترميز نفسه فقط (نفس preset/codec/جودة تمامًا) دون أي تغيير في
# الناتج النهائي. سقف 8 لتفادي عوائد متناقصة/تنافس مع عمليات أخرى.
_ENCODE_THREADS = max(2, min(os.cpu_count() or 2, 8))

import numpy as np
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import ImageFont, ImageDraw

# توافقية Pillow>=10 مع moviepy 1.0.3: الإصدارات الحديثة من Pillow أزالت
# PIL.Image.ANTIALIAS (كان يشير إلى LANCZOS)، بينما moviepy القديمة ما زالت
# تستخدمه داخليًا في fx/resize.py، فيفشل بـ AttributeError دون هذا الترقيع.
from PIL import Image as _PILImage
if not hasattr(_PILImage, "ANTIALIAS"):
    _PILImage.ANTIALIAS = _PILImage.Resampling.LANCZOS

from moviepy.editor import (
    VideoFileClip, CompositeVideoClip, concatenate_videoclips,
    AudioFileClip, ImageClip, CompositeAudioClip, vfx,
)

from config import (
    EXPORT_RESOLUTION, FADE_DURATION,
    MAX_CLIP_SLOWDOWN_STRETCH_RATIO, MIN_SOURCE_SECONDS_FOR_BOOMERANG,
    IMAGE_KEN_BURNS_ZOOM_RATIO,
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
_SUBTITLE_STROKE_WIDTH = 2
_SUBTITLE_LINE_SPACING = 10  # مسافة رأسية إضافية بين الأسطر (بالبكسل)


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


def _render_subtitle_image(text: str, font_size: int = _SUBTITLE_FONT_SIZE):
    """
    يرسم الترجمة كصورة PNG شفافة (RGBA) عبر PIL مباشرة، بدل TextClip الذي
    يعتمد داخليًا على ImageMagick. السبب: عندما يحتوي النص على أسطر متعددة
    (حالنا دائمًا بسبب _wrap_arabic_by_pixel_width)، يكتب moviepy النص لملف
    مؤقت ويمرره لـ ImageMagick بصيغة "@ملف"، وهو نمط تحظره سياسة ImageMagick
    الافتراضية (policy.xml) على أغلب توزيعات لينكس بلا أي صلاحية جذر أو
    تعديل ملفات نظام — وهذا بالضبط سبب خطأ
    "convert-im6.q16 ... not allowed by the security policy `@...`".
    بالرسم عبر PIL مباشرة (نفس المكتبة المستخدمة أصلاً في قياس عرض النص
    داخل _wrap_arabic_by_pixel_width) يصبح مسار الترجمة بالكامل بلا أي
    اعتماد على ImageMagick، فلا حاجة لتعديل أي إعداد نظام على السيرفر.
    النص يصل هنا مُهيَّأً عربيًا (reshape+bidi) ومُقسَّمًا لأسطر مسبقًا عبر
    _wrap_arabic_by_pixel_width، فلا نعيد أي معالجة على ترتيب الأحرف هنا.
    """
    font_path = _resolve_font_path()
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = None
    if font is None:
        font = ImageFont.load_default()

    display_text = text or " "

    # قياس صندوق النص الكلي (كل الأسطر معًا) عبر multiline_textbbox قبل
    # تحديد مقاس الكانفاس النهائي، بدل حساب يدوي عرضة للأخطاء.
    tmp_img = _PILImage.new("RGBA", (1, 1))
    tmp_draw = ImageDraw.Draw(tmp_img)
    bbox = tmp_draw.multiline_textbbox(
        (0, 0), display_text, font=font,
        stroke_width=_SUBTITLE_STROKE_WIDTH, spacing=_SUBTITLE_LINE_SPACING,
        align="center",
    )
    # تصحيح: عندما يكون محرك raqm متوفرًا في Pillow (يُفعَّل تلقائيًا مع
    # النصوص المعقدة/ثنائية الاتجاه كالعربية)، تُعاد بعض إحداثيات bbox
    # كأعداد عشرية (float) بدل صحيحة (مثال: -2.0 بدل -2) — بينما
    # Image.new أدناه يتطلب مقاسًا صحيحًا (int) حصرًا. هذا بالضبط سبب
    # خطأ "'float' object cannot be interpreted as an integer" الذي كان
    # يظهر عند تجميع/تصدير الفيديو بمجرد وصول الكود لأول ترجمة (subtitle).
    # نقرّب كل قيم bbox لأقرب عدد صحيح فور الحصول عليه لتفادي المشكلة
    # جذريًا في كل الاستخدامات اللاحقة لها (canvas وموضع الرسم كليهما).
    bbox = tuple(round(v) for v in bbox)
    pad = _SUBTITLE_STROKE_WIDTH * 2
    canvas_w = max(bbox[2] - bbox[0] + pad * 2, 1)
    canvas_h = max(bbox[3] - bbox[1] + pad * 2, 1)

    img = _PILImage.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (pad - bbox[0], pad - bbox[1]), display_text, font=font, fill="white",
        stroke_width=_SUBTITLE_STROKE_WIDTH, stroke_fill="black",
        spacing=_SUBTITLE_LINE_SPACING, align="center",
    )
    return np.array(img)


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


def _ken_burns_image_clip(image_path: str, duration: float, target_size: tuple[int, int]):
    """
    يبني مقطع "زوم بطيء" (Ken Burns) لصورة ثابتة واحدة بطول duration: نكبّر
    الصورة الأصلية بنسبة أكبر من إطار الهدف قليلاً (headroom)، ثم نطبّق تكبيرًا
    تدريجيًا (resize بدالة زمنية) فوق قماش (CompositeVideoClip) بحجم الهدف
    الثابت، فتُقصّ الحواف الزائدة تلقائيًا مع تحريك/تكبير الصورة - هذا الأسلوب
    القياسي في moviepy لإنتاج حركة زوم سلسة من صورة ثابتة.
    """
    headroom = 1.15  # هامش إضافي كي لا تظهر حواف فارغة أثناء الزوم
    base = ImageClip(image_path).set_duration(duration)
    base = base.resize(height=int(target_size[1] * headroom))
    if base.w < target_size[0] * headroom:
        base = base.resize(width=int(target_size[0] * headroom))

    zoom_ratio = IMAGE_KEN_BURNS_ZOOM_RATIO
    zoomed = base.resize(lambda t: 1 + (zoom_ratio - 1) * (t / duration if duration > 0 else 0))
    zoomed = zoomed.set_position("center")
    return CompositeVideoClip([zoomed], size=target_size).set_duration(duration)


def _build_image_sequence_clip(scene_result: dict, target_size: tuple[int, int]):
    """
    يبني تسلسل 3-4 صور (خطة الصور البديلة الصارمة من ai_media_verification)
    تُعرض بالتتابع مع زوم بطيء لكل صورة، بحيث يغطي التسلسل الكامل بالضبط
    audio_duration المطلوب للمشهد (نفس منطق التزامن المستخدم مع الفيديو
    العادي)، مع تلاشٍ بسيط بين الصور المتتالية لسلاسة الانتقال.
    """
    images = scene_result["images"]
    total_duration = scene_result.get("audio_duration") or 3.0
    n = len(images)
    per_image_duration = total_duration / n

    # ملاحظة مهمة عن التزامن: جرّبنا في البداية تلاشيًا متبادلاً (crossfade)
    # بين الصور عبر padding سالب في concatenate_videoclips (كما هو مستخدم بين
    # المشاهد في compose_video)، لكن اتضح عمليًا أن هذا يُنقص إجمالي مدة
    # التسلسل بمقدار غير ثابت (أكبر من مجموع الـ paddings المتوقع)، ما يكسر
    # تزامن الصوت/الصورة الذي يعتمد عليه باقي النظام بدقة الثانية. لذلك
    # التسلسل هنا قطع مباشر (hard cut) بين الصور بدل التلاشي المتبادل، لضمان
    # أن مجموع الأطوال = audio_duration تمامًا دون أي انحراف - حركة الزوم
    # البطيء (Ken Burns) نفسها تبقى موجودة على كل صورة على حدة، وهي مصدر
    # الحيوية البصرية الأساسي هنا.
    image_clips = [
        _ken_burns_image_clip(img_path, per_image_duration, target_size)
        for img_path in images
    ]
    sequence = image_clips[0] if len(image_clips) == 1 else concatenate_videoclips(image_clips)

    # تصحيح دقيق لأي فارق تقريب صغير جدًا (أجزاء من الميلي ثانية) بين مجموع
    # per_image_duration وaudio_duration الأصلي.
    if abs(sequence.duration - total_duration) > 0.01:
        if sequence.duration > total_duration:
            sequence = sequence.subclip(0, total_duration)
        else:
            sequence = sequence.set_duration(total_duration)
    return sequence


def _prepare_clip(scene_result: dict, target_size: tuple[int, int],
                   is_first: bool = False, is_last: bool = False):
    if scene_result.get("media_type") == "image_sequence":
        clip = _build_image_sequence_clip(scene_result, target_size)
        if is_first:
            clip = clip.fadein(FADE_DURATION)
        else:
            clip = clip.crossfadein(FADE_DURATION)
        if is_last:
            clip = clip.fadeout(FADE_DURATION)
        return clip

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
        "media_type": scene_result.get("media_type", "video"),
        "images": scene_result.get("images"),
        "ken_burns_zoom": IMAGE_KEN_BURNS_ZOOM_RATIO,
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
        preset="veryfast", threads=_ENCODE_THREADS, logger=None,
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

    # تصحيح: شاشة سوداء تامة تظهر في نهاية الفيديو بينما التعليق الصوتي
    # يستمر بالحديث. السبب: video.set_duration(narration.duration) كانت
    # تُغيّر فقط قيمة "المدة" المُعلَنة (metadata) للفيديو المُركَّب لتطابق
    # مدة ملف الصوت الكامل، دون أن تُمدِّد المحتوى المرئي الفعلي بأي شكل.
    # مدة الفيديو الفعلية (video.duration الطبيعية من concatenate_videoclips)
    # قد تقصر عن مدة الصوت الكامل (narration.duration) لعدة أسباب مجتمعة:
    # (1) تراكب الـ crossfade بين كل مشهدين يُنقِص (FADE_DURATION) من
    # الزمن الإجمالي في كل انتقال (بسبب padding=-FADE_DURATION أعلاه)،
    # و(2) احتمال عدم مطابقة كاملة بين نص كل مشهد وword boundaries
    # الفعلية من edge-tts داخل compute_scene_timings، مما قد يجعل توقيت
    # آخر مشهد/مشاهد ينتهي قبل نهاية الصوت الفعلية بقليل أو كثير. أي جزء
    # من narration.duration يتجاوز طول الفيديو الفعلي كان يُعرَض كإطارات
    # سوداء تمامًا (خلفية CompositeVideoClip الافتراضية) لأن moviepy لا
    # يملك أي محتوى مرئي لرسمه هناك. الحل: لو كان الفيديو أقصر من الصوت،
    # نُجمِّد آخر إطار (نفس أسلوب _extend_clip_to_duration أعلاه لكل مشهد
    # منفرد) ليغطي الفارق بدل تركه أسود بالكامل؛ ولو كان أطول (نادر)،
    # نقصّه لمدة الصوت بدل تمديد duration وهميًا بلا محتوى.
    gap = narration.duration - video.duration
    if gap > 0.05:
        logger.warning(
            "مدة الفيديو المُركَّب (%.2fث) أقصر من مدة الصوت الكامل (%.2fث) "
            "بفارق %.2fث — سيُجمَّد آخر إطار لتغطية الفارق بدل شاشة سوداء. "
            "يُستحسن مراجعة تزامن compute_scene_timings إن تكرر فارق كبير.",
            video.duration, narration.duration, gap,
        )
        video = video.fx(vfx.freeze, t="end", total_duration=narration.duration)
    elif gap < -0.05:
        video = video.subclip(0, narration.duration)

    video = video.set_audio(narration)

    subtitle_clips = []
    for seg in subtitle_segments:
        wrapped_text = _wrap_arabic_by_pixel_width(
            seg["text"], max_width_px=int(size[0] * 0.9), font_size=_SUBTITLE_FONT_SIZE
        )
        # صورة PIL شفافة بدل TextClip/ImageMagick (انظر شرح _render_subtitle_image
        # أعلاه) — تتجنب نهائيًا خطأ ImageMagick policy على النصوص متعددة الأسطر.
        subtitle_array = _render_subtitle_image(wrapped_text, font_size=_SUBTITLE_FONT_SIZE)
        txt = (
            ImageClip(subtitle_array, transparent=True)
            .set_start(seg["start"])
            .set_end(seg["end"])
            .set_position(("center", "bottom"))
        )
        subtitle_clips.append(txt)

    final = CompositeVideoClip([video] + subtitle_clips, size=size)
    final.write_videofile(
        out_path, fps=30, codec="libx264", audio_codec="aac",
        preset="veryfast", threads=_ENCODE_THREADS,
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
