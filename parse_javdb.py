import cloudscraper
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import json
import time
import os
import base64
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
import logging
import sys
import random
from collections import defaultdict

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
BASE_URL = "https://www.javdatabase.com"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap_index.xml"
CACHE_FILE = "sitemap_cache.json"
DATA_DIR = "data"
METADATA_FILE = "metadata.json"

# Тестовый режим
TEST_MODE = os.environ.get('TEST_MODE', 'false').lower() == 'true'
TEST_SITEMAP_LIMIT = int(os.environ.get('TEST_SITEMAP_LIMIT', '1'))
TEST_FILM_LIMIT = int(os.environ.get('TEST_FILM_LIMIT', '5'))

# Ключ из GitHub Secrets (с fallback для локальной разработки)
XOR_KEY = os.environ.get('XOR_KEY', 'local_dev_fallback_key_change_me')

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
]

REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

# --- Шифрование ---

def xor_encrypt_decrypt(data: bytes, key: str) -> bytes:
    key_bytes = key.encode('utf-8')
    result = bytearray(len(data))
    for i in range(len(data)):
        result[i] = data[i] ^ key_bytes[i % len(key_bytes)]
    return bytes(result)

def save_encrypted(data: dict, filepath: str, key: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    encrypted = xor_encrypt_decrypt(json_str.encode('utf-8'), key)
    with open(filepath, 'wb') as f:
        f.write(base64.b64encode(encrypted))

def load_encrypted(filepath: str, key: str) -> dict:
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        encrypted = base64.b64decode(f.read())
    return json.loads(xor_encrypt_decrypt(encrypted, key).decode('utf-8'))

# --- Scraper ---

def create_scraper():
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
        delay=10
    )
    scraper.headers.update({
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    })
    return scraper

def fetch_with_retry(scraper, url, max_retries=MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                scraper.headers.update({'User-Agent': random.choice(USER_AGENTS)})
                time.sleep(random.uniform(2, 5))
            
            response = scraper.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                if response.encoding == 'ISO-8859-1':
                    response.encoding = 'utf-8'
                return response
            elif response.status_code == 403:
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(5, 10))
                    
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(random.uniform(1, 3))
    
    return None

# --- Sitemap ---

def get_sitemap_urls():
    scraper = create_scraper()
    
    logger.info(f"Загрузка sitemap-индекса: {SITEMAP_INDEX_URL}")
    
    resp = fetch_with_retry(scraper, SITEMAP_INDEX_URL)
    if not resp:
        logger.error("Не удалось получить sitemap index")
        return []
    
    root = ET.fromstring(resp.content)
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    sitemaps = []
    
    for sitemap in root.findall('sm:sitemap', ns):
        loc_elem = sitemap.find('sm:loc', ns)
        if loc_elem is not None:
            sitemaps.append({'loc': loc_elem.text})

    movie_sitemaps = [s for s in sitemaps if 'movies-sitemap' in s['loc']]
    logger.info(f"Найдено {len(movie_sitemaps)} movies-sitemap файлов")
    
    if TEST_MODE:
        movie_sitemaps = movie_sitemaps[:TEST_SITEMAP_LIMIT]
        logger.info(f"ТЕСТ: обрабатываем {len(movie_sitemaps)} sitemap(ов)")
    
    all_film_paths = []
    
    for i, sitemap_data in enumerate(movie_sitemaps, 1):
        loc = sitemap_data['loc']
        logger.info(f"[{i}/{len(movie_sitemaps)}] {loc}")
        
        time.sleep(random.uniform(1, 2))
        resp = fetch_with_retry(scraper, loc)
        
        if not resp:
            continue
        
        try:
            sitemap_root = ET.fromstring(resp.content)
            
            for url in sitemap_root.findall('sm:url', ns):
                film_loc_elem = url.find('sm:loc', ns)
                if film_loc_elem is not None:
                    parsed_url = urlparse(film_loc_elem.text)
                    if '/movies/' in parsed_url.path:
                        all_film_paths.append(parsed_url.path)
                        
                        if TEST_MODE and len(all_film_paths) >= TEST_FILM_LIMIT:
                            break
            
            logger.info(f"  -> URL: {len(all_film_paths)}")
            
            if TEST_MODE and len(all_film_paths) >= TEST_FILM_LIMIT:
                break
                
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    if TEST_MODE:
        all_film_paths = all_film_paths[:TEST_FILM_LIMIT]
    
    logger.info(f"Итого URL: {len(all_film_paths)}")
    return all_film_paths

# --- Парсинг фильма ---

def parse_film_page(scraper, url_path):
    full_url = urljoin(BASE_URL, url_path)
    
    resp = fetch_with_retry(scraper, full_url)
    if not resp:
        return None
    
    if resp.encoding == 'ISO-8859-1':
        resp.encoding = 'utf-8'
    
    soup = BeautifulSoup(resp.text, 'html.parser')
    film_data = {}
    
    # Код
    path_parts = url_path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[-2] == 'movies':
        film_data['code'] = path_parts[-1].upper()
    else:
        return None

    # Название
    title = None
    h1 = soup.find('h1')
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].strip()
    if not title:
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True).replace(' - JAV Database', '')
    film_data['title'] = title or 'No Title'

    # Описание
    desc = ''
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        desc = meta_desc['content'].strip()
    film_data['description'] = desc[:500]

    # Обложка
    thumb = None
    og_img = soup.find('meta', property='og:image')
    if og_img and og_img.get('content'):
        thumb = urlparse(og_img['content']).path
    if not thumb:
        poster = soup.find('div', id='poster-container')
        if poster:
            img = poster.find('img')
            if img and img.get('src'):
                thumb = urlparse(urljoin(BASE_URL, img['src'])).path
    film_data['thumbnail'] = thumb

    # Скриншоты
    screenshots = []
    gallery = soup.find('div', class_='image-gallery-section')
    if gallery:
        for a in gallery.find_all('a', attrs={'data-image-src': True}):
            screenshots.append(urlparse(a['data-image-src']).path)
    film_data['screenshots'] = screenshots[:10]

    # Метаданные
    genres = []
    actresses = []
    movie_table = soup.find('div', class_='movietable')
    if movie_table:
        for row in movie_table.find_all(['p', 'div']):
            text = row.get_text(strip=True)
            if 'Genre(s):' in text:
                genres = [a.get_text(strip=True) for a in row.find_all('a', rel='tag')]
            if 'Idol(s)/Actress(es):' in text:
                actresses = [a.get_text(strip=True) for a in row.find_all('a')]

    film_data['metadata'] = {
        'genre': genres[:10],
        'actress': actresses[:10]
    }

    # Дата релиза
    release_date = None
    if movie_table:
        for row in movie_table.find_all(['p', 'div']):
            text = row.get_text(strip=True)
            if 'Release Date:' in text:
                date_str = text.split('Release Date:')[-1].strip()
                try:
                    datetime.strptime(date_str, '%Y-%m-%d')
                    release_date = date_str
                except:
                    pass
    film_data['releaseDate'] = release_date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    return film_data

# --- Сохранение ---

def save_films_by_month(films, key):
    films_by_month = defaultdict(list)
    
    for film in films:
        if film.get('releaseDate'):
            month_key = film['releaseDate'][:7]
            films_by_month[month_key].append(film)
    
    for month, month_films in films_by_month.items():
        filepath = os.path.join(DATA_DIR, f"{month}.bin")
        
        existing = load_encrypted(filepath, key)
        existing_films = existing.get('films', []) if existing else []
        
        codes = {f['code'] for f in existing_films}
        new = [f for f in month_films if f['code'] not in codes]
        all_films = existing_films + new
        
        data = {
            "films": all_films,
            "metadata": {
                "version": "1.0.0",
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "month": month,
                "totalFilms": len(all_films)
            }
        }
        
        save_encrypted(data, filepath, key)
        logger.info(f"  💾 {month}.bin: {len(all_films)} фильмов (+{len(new)} новых)")

# --- Главная ---

def main():
    start_time = time.time()
    logger.info("="*60)
    logger.info("Парсер JAVDatabase")
    if TEST_MODE:
        logger.info(f"ТЕСТ: {TEST_SITEMAP_LIMIT} sitemap, {TEST_FILM_LIMIT} фильмов")
    logger.info("="*60)
    
    film_paths = get_sitemap_urls()
    
    if not film_paths:
        logger.warning("Нет URL")
        return
    
    # Загружаем существующие коды
    existing_codes = set()
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.endswith('.bin'):
                data = load_encrypted(os.path.join(DATA_DIR, f), XOR_KEY)
                if data:
                    for film in data.get('films', []):
                        existing_codes.add(film['code'])
    logger.info(f"В базе: {len(existing_codes)} фильмов")
    
    # Новые
    new_paths = []
    for path in film_paths:
        code = path.strip('/').split('/')[-1].upper()
        if code not in existing_codes:
            new_paths.append(path)
    
    logger.info(f"Новых: {len(new_paths)}")
    
    if not new_paths:
        logger.info("Нет новых фильмов")
        return
    
    # Парсим
    scraper = create_scraper()
    new_films = []
    
    for i, path in enumerate(new_paths, 1):
        logger.info(f"[{i}/{len(new_paths)}] {path}")
        
        film = parse_film_page(scraper, path)
        
        if film:
            new_films.append(film)
            logger.info(f"  ✓ {film['code']}: {film['title'][:60]}")
        else:
            logger.warning(f"  ✗ Ошибка")
        
        time.sleep(random.uniform(1.5, 3))
    
    # Сохраняем
    save_films_by_month(new_films, XOR_KEY)
    
    # Метаданные
    total = len(existing_codes) + len(new_films)
    metadata = {
        "lastUpdate": datetime.now(timezone.utc).isoformat(),
        "totalFilms": total,
        "newFilms": len(new_films)
    }
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    # Кэш sitemap
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump({"lastRun": datetime.now(timezone.utc).isoformat()}, f)
    
    logger.info(f"Готово! Новых: {len(new_films)}, всего: {total}")
    logger.info(f"Время: {(time.time()-start_time)/60:.1f} мин")

if __name__ == "__main__":
    main()
