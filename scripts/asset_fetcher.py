"""
asset_fetcher.py
تم إيقاف Pexels مؤقتاً والاعتماد حصرياً على Pixabay.
"""
import requests
from requests.utils import quote
from scripts import config

MIN_WIDTH = 1920

def fetch_pixabay(keyword: str, per_page: int = 3) -> list[str]:
    if not config.PIXABAY_API_KEY:
        return []
    
    encoded_keyword = quote(keyword)
    # استخدام Pixabay فقط وتجنب أي وسائط قد تسبب خطأ 400
    url = f"https://pixabay.com/api/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}&image_type=photo&orientation=horizontal&per_page={per_page}&safesearch=true"
    
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        return [h["largeImageURL"] for h in hits if h.get("imageWidth", 0) >= MIN_WIDTH]
    except Exception as e:
        print(f"[ASSET ERROR] فشل Pixabay لـ '{keyword}': {e}")
        return []

def get_images_for_scene(keywords: list[str], target_count: int = 3) -> list[str]:
    """يعتمد على Pixabay حصرياً لتفادي أخطاء Pexels."""
    images = []
    # محاولة الجلب فقط من Pixabay
    for kw in keywords:
        images += fetch_pixabay(kw, per_page=target_count)
        if len(images) >= target_count:
            break
            
    # إذا لم نجد صوراً، نستخدم بدائل عامة ثابتة لضمان عدم توقف النظام
    if not images:
        return ["https://pixabay.com/get/g60f5..."] # (استخدم رابط صورة افتراضية كـ Placeholder)
        
    return images[:target_count]

def download_image(url: str, out_path: str):
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return out_path
    except Exception:
        return None
