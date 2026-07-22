"""
thumbnail_generator.py
تم إعادة بناؤه بالكامل — السبب: تأكدنا (من تجربتك الفعلية + توثيق Google
الحالي) أن نماذج توليد الصور بـ Gemini API (Nano Banana وNano Banana Pro)
لا تملك أي حصة مجانية بالـ API إطلاقاً (0 RPD على الطبقة المجانية)، بعكس
تطبيق Gemini نفسه الذي عنده حصة مجانية محدودة. هذا ليس خطأ بالكود، هو قيد
حقيقي بخطة Google المجانية.

البديل (اقتراحك، وهو الصحيح): تحميل صورة خلفية حقيقية من Pixabay (نفس مصدر
صور الفيديو، بدون تكلفة إضافية) وتركيب نص جذاب فوقها برمجياً عبر Pillow،
بأبعاد مطابقة لاتجاه الفيديو (عمودي 1080x1920 للشورت، أفقي 1280x720 للطويل).
"""
import io
import json
import textwrap

import requests
from PIL import Image, ImageDraw, ImageFont

from scripts import config, gemini_client, asset_fetcher
from scripts.telegram_alerts import alert_key_error

# خط عريض متوفر افتراضياً على GitHub Actions Ubuntu runner (نثبته صراحة
# بخطوة "fonts-dejavu-core" بملف الـ workflow لضمان وجوده دائماً)
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

HOOK_PROMPT = """
من السكربت التالي، استخرج:
1. جملة "صادمة" من 3 إلى 6 كلمات إنجليزية فقط، بخط ضخم، أسلوب فضول قوي
   وغير مضلل، تصلح كنص على thumbnail يوتيوب
2. كلمة بحث إنجليزية واحدة أو اثنتين (لصورة خلفية) تمثل الموضوع بصرياً

السكربت: {script_text}

أرجع JSON فقط: {{"thumbnail_text": "...", "background_keyword": "..."}}
"""


def _extract_hook_and_keyword(script_text: str, main_topic: str) -> dict:
    try:
        raw = gemini_client.generate_text(
            HOOK_PROMPT.format(script_text=script_text[:1500]),
            model=config.MODEL_SEO, key_type="light", json_mode=True,
        )
        data = json.loads(raw)
        if not data.get("thumbnail_text"):
            raise ValueError("رجع بدون thumbnail_text")
        return data
    except Exception as e:
        # نظام إنقاذ: لو فشل Gemini، نستخدم الموضوع نفسه كنص احتياطي
        print(f"[THUMBNAIL WARNING] فشل استخراج الـ hook عبر Gemini: {e}. استخدام احتياطي.")
        return {"thumbnail_text": main_topic[:30].upper(), "background_keyword": main_topic}


def _fetch_background_image(keyword: str, is_short: bool) -> Image.Image:
    orientation = "vertical" if is_short else "horizontal"
    urls = asset_fetcher.fetch_pixabay(keyword, per_page=3, orientation=orientation)
    if not urls:
        for fallback in ["abstract background", "nature", "gradient"]:
            urls = asset_fetcher.fetch_pixabay(fallback, per_page=3, orientation=orientation)
            if urls:
                break
    if not urls:
        raise RuntimeError("تعذر إيجاد أي صورة خلفية للغلاف حتى بالكلمات الاحتياطية")

    r = requests.get(urls[0], timeout=20)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _cover_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """يقصّ الصورة لتملأ الأبعاد المطلوبة تماماً بدون تمطيط يشوّه النسب
    (نفس منطق object-fit: cover بالـ CSS)."""
    src_ratio = img.width / img.height
    target_ratio = target_w / target_h
    if src_ratio > target_ratio:
        new_height = target_h
        new_width = int(src_ratio * new_height)
    else:
        new_width = target_w
        new_height = int(new_width / src_ratio)
    img = img.resize((new_width, new_height), Image.LANCZOS)
    left = (new_width - target_w) // 2
    top = (new_height - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _draw_outlined_text(draw, xy, text, font, fill, outline_color, outline_width):
    x, y = xy
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def build_thumbnail(script_text: str, main_topic: str, out_path: str,
                     is_short: bool = True) -> str:
    target_w, target_h = (1080, 1920) if is_short else (1280, 720)

    extraction = _extract_hook_and_keyword(script_text, main_topic)
    hook_text = extraction["thumbnail_text"].upper()
    keyword = extraction.get("background_keyword") or main_topic

    bg = _fetch_background_image(keyword, is_short)
    bg = _cover_crop(bg, target_w, target_h).convert("RGBA")

    # تعتيم جزء من الصورة (أسفل للشورت، أعلى للطويل) حتى يبقى النص واضحاً
    # فوق أي خلفية، بدل خط ثابت اللون قد يضيع فوق صورة فاتحة أو مزدحمة
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    if is_short:
        zone = (0, int(target_h * 0.55), target_w, target_h)
    else:
        zone = (0, 0, target_w, int(target_h * 0.45))
    draw_overlay.rectangle(zone, fill=(0, 0, 0, 150))
    bg = Image.alpha_composite(bg, overlay).convert("RGB")

    draw = ImageDraw.Draw(bg)
    font_size = int(target_w * (0.11 if is_short else 0.075))
    font = ImageFont.truetype(FONT_BOLD_PATH, font_size)

    max_chars_per_line = max(6, int(target_w / (font_size * 0.58)))
    lines = textwrap.fill(hook_text, width=max_chars_per_line).split("\n")

    line_height = int(font_size * 1.15)
    total_text_height = line_height * len(lines)
    start_y = (target_h - total_text_height - 80) if is_short else 70

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        x = (target_w - line_width) // 2
        y = start_y + i * line_height
        _draw_outlined_text(
            draw, (x, y), line, font,
            fill=(255, 212, 0), outline_color=(0, 0, 0),
            outline_width=max(2, font_size // 20),
        )

    bg.save(out_path, quality=92)
    return out_path
