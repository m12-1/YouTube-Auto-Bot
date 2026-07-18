"""
asset_fetcher.py
لكل "مشهد" بالسكربت، يسحب 2-3 صور بديلة عالية الدقة من Pixabay و Pexels
حسب الكلمات المفتاحية البصرية اللي حددها script_writer.
"""
import requests
from requests.utils import quote
from scripts import config

MIN_WIDTH = 1920  # نفرض دقة كافية لفيديو 1080p

def fetch_pixabay(keyword: str, per_page: int = 3) -> list[str]:
    if not config.PIXABAY_API_KEY:
        return []
    
    # استخدام الترميز الآمن لتجنب Bad Request (400)
    encoded_keyword = quote(keyword)
    url = f"https://pixabay.com/api/?key={config.PIXABAY_API_KEY}&q={encoded_keyword}&image_type=photo&orientation=horizontal&per_page={per_page}&safesearch=true"
    
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        # تصفية برمجية للجودة للتأكد من الدقة
        return [h["largeImageURL"] for h in hits if h.get("imageWidth", 0) >= MIN_WIDTH]
    except Exception as e:
        print(f"[ASSET ERROR] فشل جلب صور Pixabay لكلمة '{keyword}': {e}")
        return []

def fetch_pexels(keyword: str, per_page: int = 3) -> list[str]:
    if not config.PEXELS_API_KEY:
        return []
    
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": keyword, "per_page": per_page, "orientation": "landscape"}
    
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        return [p["src"]["large2x"] for p in photos if p.get("width", 0) >= MIN_WIDTH]
    except Exception as e:
        print(f"[ASSET ERROR] فشل جلب صور Pexels لكلمة '{keyword}': {e}")
        return []

def get_images_for_scene(keywords: list[str], target_count: int = 3) -> list[str]:
    """يحاول Pixabay أولاً، ولو النتائج ناقصة يكمّل من Pexels."""
    images = []
    for kw in keywords:
        images += fetch_pixabay(kw, per_page=2)
        if len(images) >= target_count:
            break
            
    if len(images) < target_count:
        for kw in keywords:
            images += fetch_pexels(kw, per_page=2)
            if len(images) >= target_count:
                break
                
    return images[:target_count] or ["PLACEHOLDER_NO_IMAGE_FOUND"]

def download_image(url: str, out_path: str):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)
    return out_path
