"""
طبقة "ملاذ أخير مضمون" (Guaranteed Fallback).

المشكلة التي تحلّها: بعض المشاهد (خصوصًا حقائق رقمية/مفاهيمية مثل "243 يوم
أرضي") لا يوجد لها فيديو/صور ستوك تطابق النص بدقة 9/10 في أي مصدر مجاني
(Pexels/Pixabay/Internet Archive/Wikimedia/NASA)، مهما زادت محاولات إعادة
البحث (final_scene_audit + ai_media_verification). السقف الواقعي المتاح من
مصدر خارجي قد يكون 6-7 فقط لهذه المشاهد تحديدًا، وهذا ليس عيبًا في منطق
إعادة المحاولة بل سقف في مصدر البيانات نفسه.

الحل هنا: بدل الاستمرار في البحث عن "إبرة في كومة قش"، تُولَّد بطاقة رسومية
متحركة (Motion Graphic Card) عبر ffmpeg تطابق نص المشهد 100% لأنها مُنشأة
بالكامل برمجيًا (لا تُبحث عن مصدر خارجي):
  - خلفية متدرّجة متحركة ببطء (ffmpeg lavfi "gradients")، مع احتياط تلقائي
    للون ثابت (lavfi "color") لو كان بناء ffmpeg الحالي لا يدعم فلتر
    "gradients" (أُضيف في إصدارات حديثة نسبيًا من ffmpeg).
  - تكبير بطيء (Ken Burns) على كامل الفريم عبر "zoompan".
  - نص المشهد نفسه، مُهيَّأ عربيًا (تشكيل/ترتيب RTL) عبر نفس منطق التهيئة
    المستخدم في الترجمة (subtitle) بموديول video_composer، مرسوم كصورة PNG
    شفافة عبر PIL ثم يُدمج فوق الخلفية عبر "overlay".

تُستخدم هذه الوحدة فقط من main.py، وفقط كملاذ أخير بعد استنفاد كل جولات
التدقيق النهائي (MAX_SCENE_AUDIT_RETRIES) مع بقاء مشاهد فاشلة. النتيجة
مُصمَّمة عمدًا لتُسجَّل بدرجة ثابتة عالية (GRAPHIC_FALLBACK_SCORE) لأن مطابقتها
للنص مضمونة بالتصميم، لا تحتاج تدقيق Gemini إضافي.

ملاحظة صادقة: هذا تنازل مقصود (جودة بصرية بسيطة/بطاقة نصية بدل فيديو حقيقي)
بدل انتظار فيديو "مثالي" قد لا يوجد أصلاً لهذا النوع من المشاهد. لتخصيص شكل
البطاقة (شعار قناة، أيقونات، ألوان مختلفة حسب الفئة...) عدّل الثوابت أدناه أو
مرّر gradient_colors/solid_color لـ build_fallback_clip.
"""

import hashlib
import logging
import os
import subprocess
import tempfile

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from config import EXPORT_RESOLUTION
from shared.gemini_client import call_gemini_with_rotation, parse_json_response

logger = logging.getLogger("modules.graphic_fallback")

STAGE = "graphic_fallback"

# نص البطاقة يجب أن يكون: (1) خاصًا بمحتوى هذا المشهد تحديدًا (لا نص عام
# ثابت)، و(2) بالإنجليزية دائمًا بصرف النظر عن لغة السرد الأصلي (narration قد
# يكون عربيًا)، لأن هذه البطاقة بديل بصري "ستوك-ستايل" يفترض نصًا إنجليزيًا
# قصيرًا كما لو كان عنوانًا فوق فيديو حقائق إنجليزي عادي. لذلك تُطلب صياغة
# البطاقة من Gemini (ترجمة + تكثيف للحقيقة المحددة في هذا المشهد)، بدل عرض
# scene["text"] الخام كما هو.
_CAPTION_PROMPT = """
Full video narration (for context only): "{narration}"

This specific scene's text: "{scene_text}"

Write a short on-screen caption (English, max 12 words) that states the exact
specific fact/idea of THIS scene only (not the whole video). It must be
concrete and specific to this scene's content, not a generic placeholder.
Always output in English even if the input above is in another language.

Return ONLY JSON: {{"caption": "..."}}
"""


def get_card_caption(narration: str, scene_text: str) -> str:
    """يطلب من Gemini صياغة نص البطاقة: تعريب/تلخيص إلى إنجليزية قصيرة (≤12
    كلمة) خاصة بمضمون هذا المشهد تحديدًا. عند فشل الاستدعاء (نادر)، يُستخدم
    scene_text الخام كملاذ أخير للملاذ الأخير نفسه، حتى لا تتوقف خط الإنتاج
    بالكامل بسبب فشل هذه الخطوة الإضافية وحدها."""
    prompt = _CAPTION_PROMPT.format(narration=narration, scene_text=scene_text)
    try:
        raw = call_gemini_with_rotation(STAGE, [prompt], response_mime_type="application/json")
        caption = parse_json_response(raw).get("caption", "").strip()
        if caption:
            return caption
        logger.warning("Gemini أعاد نص بطاقة فارغًا لهذا المشهد؛ سيُستخدم نص المشهد الخام كما هو.")
    except Exception as e:  # noqa: BLE001
        logger.warning("فشل توليد نص البطاقة الإنجليزي عبر Gemini (%s)؛ سيُستخدم نص المشهد الخام كما هو.", e)
    return scene_text

# درجة ثابتة تُسجَّل لأي مشهد استُبدل ببطاقة الملاذ الأخير (تُستخدم فقط في
# سجلّات/ملفات التحليل مثل accepted_scenes_visual_analysis.json — هذه
# المشاهد لا تمر بتدقيق final_scene_audit مجددًا لأن مطابقتها مضمونة بالتصميم).
GRAPHIC_FALLBACK_SCORE = 9.2

_CARD_FONT_SIZE = 64
_CARD_LINE_SPACING = 22
_CARD_TEXT_WIDTH_RATIO = 0.82   # نسبة عرض النص من عرض الفريم (هامش على الجانبين)
_CARD_STROKE_WIDTH = 3
_CARD_MIN_DURATION = 0.8

_SUBTITLE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# ألوان الخلفية المتدرّجة الافتراضية (من الأعلى للأسفل) - أزرق داكن هادئ يناسب
# أغلب فئات "حقائق علمية/فضاء" الشائعة في هذا البوت، ويبقى مقروءًا تحت أي نص
# أبيض بحدود سوداء. قابلة للتخصيص عبر معاملات build_fallback_clip.
_DEFAULT_GRADIENT_TOP = "0x0f2027"
_DEFAULT_GRADIENT_BOTTOM = "0x2c5364"
_DEFAULT_SOLID_FALLBACK_COLOR = "0x16213e"

# سرعة حركة التدرّج الداخلية لفلتر gradients (بطيئة جدًا حتى تبدو شبه ثابتة
# ولا تُشتت الانتباه عن النص - القيمة يجب أن تكون > 0 بصرامة، ffmpeg يرفض 0).
_GRADIENT_ANIMATION_SPEED = 0.02

# نسبة التكبير الإجمالية المستهدفة عبر zoompan خلال كامل مدة المشهد (Ken Burns
# بطيء على كامل البطاقة، مطابق روحيًا لنفس الإحساس المستخدم مع IMAGE_KEN_BURNS_ZOOM_RATIO
# في المشاهد المبنية من صور ثابتة).
_ZOOM_TARGET_RATIO = 1.15
_ZOOM_STEP_PER_FRAME = 0.0006


def _ffmpeg_supports_gradients() -> bool:
    """يفحص عبر `ffmpeg -filters` إن كان بناء ffmpeg الحالي يدعم مصدر lavfi
    "gradients" (أُضيف في إصدارات حديثة نسبيًا). يعيد False بأمان لأي خطأ
    (ffmpeg غير متاح، أمر غير مدعوم...) بدل رفع استثناء، حتى يبقى المسار
    الاحتياطي (لون ثابت) هو النتيجة الآمنة الافتراضية."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
        return " gradients " in f" {result.stdout} " or "gradients" in result.stdout
    except Exception as e:  # noqa: BLE001
        logger.warning("تعذّر فحص دعم فلتر gradients في ffmpeg (%s)، سيُستخدم لون ثابت احتياطيًا.", e)
        return False


def _contains_arabic(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06FF" for ch in text)


def _shape_arabic(text: str) -> str:
    if not _contains_arabic(text):
        return text
    return get_display(arabic_reshaper.reshape(text))


def _resolve_font(font_size: int) -> ImageFont.FreeTypeFont:
    for path in _SUBTITLE_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue
    logger.warning("تعذّر إيجاد أي خط TTF من المرشحات المعروفة؛ سيُستخدم خط PIL الافتراضي (قد لا يدعم العربية جيدًا).")
    return ImageFont.load_default()


def _wrap_by_pixel_width(text: str, font: ImageFont.FreeTypeFont, max_width_px: int) -> list[str]:
    """يقسّم النص لأسطر بحيث يطابق كل سطر عرض البطاقة الفعلي بالبكسل، مع
    تهيئة عربية (reshape + bidi) مستقلة لكل سطر على حدة (نفس منطق
    video_composer._wrap_arabic_by_pixel_width) حتى لا يُكسر ترتيب الأحرف
    عند لفّ لاحق بمنطق LTR عادي."""
    words = text.split()
    if not words:
        return []

    def shaped_width(candidate_words: list[str]) -> float:
        raw_line = " ".join(candidate_words)
        shaped = _shape_arabic(raw_line)
        return font.getlength(shaped)

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
    return lines


def _render_text_overlay_png(text: str, size: tuple[int, int], out_path: str) -> None:
    """يرسم نص المشهد كصورة PNG شفافة (RGBA) بعرض/ارتفاع الفريم كاملاً، بنص
    أبيض بحدود سوداء (مقروء فوق أي جزء من التدرّج/اللون الثابت)، متوسّطًا
    رأسيًا وأفقيًا."""
    w, h = size
    font = _resolve_font(_CARD_FONT_SIZE)
    max_width_px = int(w * _CARD_TEXT_WIDTH_RATIO)
    lines = _wrap_by_pixel_width(text, font, max_width_px)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    line_metrics = []
    total_h = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=_CARD_STROKE_WIDTH)
        line_h = bbox[3] - bbox[1]
        line_metrics.append((line, bbox, line_h))
        total_h += line_h
    if len(lines) > 1:
        total_h += _CARD_LINE_SPACING * (len(lines) - 1)

    y = (h - total_h) // 2
    for line, bbox, line_h in line_metrics:
        line_w = bbox[2] - bbox[0]
        x = (w - line_w) // 2 - bbox[0]
        draw.text(
            (x, y - bbox[1]), line, font=font,
            fill=(255, 255, 255, 255),
            stroke_width=_CARD_STROKE_WIDTH, stroke_fill=(0, 0, 0, 255),
        )
        y += line_h + _CARD_LINE_SPACING

    img.save(out_path)


def build_fallback_clip(
    scene_text: str,
    duration: float,
    out_path: str,
    size: tuple[int, int] = None,
    gradient_colors: tuple[str, str] = None,
    solid_color: str = None,
) -> str:
    """يولّد فيديو mp4 (بطاقة رسومية متحركة) لمدة `duration` ثانية بالضبط،
    يطابق `scene_text` كنص مركزي، ويكتبه في `out_path`. يعيد `out_path` عند
    النجاح، ويرفع الاستثناء الأصلي لو فشل ffmpeg (يتولى main.py قرار
    الاستمرار/عدمه في حال الفشل النادر لهذا الملاذ الأخير نفسه).
    """
    size = size or EXPORT_RESOLUTION
    w, h = size
    duration = max(float(duration), _CARD_MIN_DURATION)
    top_color, bottom_color = gradient_colors or (_DEFAULT_GRADIENT_TOP, _DEFAULT_GRADIENT_BOTTOM)
    solid_color = solid_color or _DEFAULT_SOLID_FALLBACK_COLOR

    text_png_path = os.path.join(
        tempfile.gettempdir(),
        f"graphic_fallback_text_{hashlib.sha1(scene_text.encode('utf-8')).hexdigest()[:16]}.png",
    )
    _render_text_overlay_png(scene_text, size, text_png_path)

    if _ffmpeg_supports_gradients():
        bg_filter = (
            f"gradients=size={w}x{h}:duration={duration}:speed={_GRADIENT_ANIMATION_SPEED}:"
            f"c0={top_color}:c1={bottom_color}:x0=0:y0=0:x1=0:y1={h}"
        )
    else:
        logger.warning(
            "بناء ffmpeg الحالي لا يدعم فلتر lavfi 'gradients'؛ استخدام خلفية بلون ثابت احتياطيًا."
        )
        bg_filter = f"color=c={solid_color}:size={w}x{h}:duration={duration}"

    zoom_expr = f"min(zoom+{_ZOOM_STEP_PER_FRAME},{_ZOOM_TARGET_RATIO})"
    filter_complex = (
        f"[0:v]zoompan=z='{zoom_expr}':d=1:s={w}x{h}:fps=30,format=yuv420p[bg];"
        f"[1:v]format=rgba[txt];"
        f"[bg][txt]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", bg_filter,
        "-loop", "1", "-i", text_png_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-t", str(duration), "-r", "30", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast",
        out_path,
    ]
    try:
        subprocess.run(
            cmd, check=True, timeout=180,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="ignore") if e.stderr else ""
        logger.error("فشل توليد بطاقة الملاذ الأخير عبر ffmpeg: %s", stderr[-1500:])
        raise
    finally:
        try:
            os.remove(text_png_path)
        except OSError:
            pass

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("ffmpeg لم ينتج ملف فيديو صالح لبطاقة الملاذ الأخير.")
    return out_path


def build_fallback_scene_result(scene: dict, duration: float, out_dir: str, narration: str = "") -> dict:
    """يبني قاموس scene_result متوافق مع الشكل الذي يتوقعه video_composer
    (نفس مفاتيح مشهد الفيديو العادي: media_type/clip_path/start/end)، بحيث
    يُعامَل كأي مشهد فيديو طبيعي في حلقة المونتاج/الكاش دون أي تعديل إضافي
    على video_composer.py نفسه.

    نص البطاقة المعروض ليس scene["text"] الخام، بل نص مُولَّد عبر
    get_card_caption: خاص بهذا المشهد تحديدًا (ليس نصًا عامًا ثابتًا) وبالإنجليزية
    دائمًا (انظر get_card_caption لمزيد من التفاصيل).
    """
    os.makedirs(out_dir, exist_ok=True)
    clip_path = os.path.join(out_dir, f"{scene['id']}_graphic_fallback.mp4")
    caption = get_card_caption(narration or scene["text"], scene["text"])
    build_fallback_clip(caption, duration, clip_path)
    return {
        "media_type": "video",
        "clip_path": clip_path,
        "start": 0,
        "end": duration,
        "audio_duration": duration,
        "score": GRAPHIC_FALLBACK_SCORE,
        "is_graphic_fallback": True,
        "needs_manual_review": False,
    }
