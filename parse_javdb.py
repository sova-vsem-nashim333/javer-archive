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
OUTPUT_FILE = "parsed_films.json"
ENCRYPTED_FILE = "parsed_films.enc"

# Тестовый режим
TEST_MODE = os.environ.get('TEST_MODE', 'false').lower() == 'true'
TEST_SITEMAP_LIMIT = int(os.environ.get('TEST_SITEMAP_LIMIT', '1'))  # Сколько sitemap'ов парсить
TEST_FILM_LIMIT = int(os.environ.get('TEST_FILM_LIMIT', '5'))  # Сколько фильмов парсить

# Ключ для XOR шифрования
XOR_KEY = os.environ.get('XOR_KEY', '299af363382d01e6ad36ddca7fa39ca92ee1627efe733dc6')

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
]

REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

# --- Функции шифрования ---

def xor_encrypt_decrypt(data: bytes, key: str) -> bytes:
    key_bytes = key.encode('utf-8')
    result = bytearray(len(data))
    for i in range(len(data)):
        result[i] = data[i] ^ key_bytes[i % len(key_bytes)]
    return bytes(result)

def save_encrypted_json(data: dict, filepath: str, key: str):
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    encrypted = xor_encrypt_decrypt(json_str.encode('utf-8'), key)
    with open(filepath, 'wb') as f:
        f.write(base64.b64encode(encrypted))

def load_encrypted_json(filepath: str, key: str) -> dict:
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        encrypted = base64.b64decode(f.read())
    return json.loads(xor_encrypt_decrypt(encrypted, key).decode('utf-8'))

# --- Основные функции ---

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
                # Фикс кодировки
                if response.encoding == 'ISO-8859-1':
                    response.encoding = 'utf-8'
                return response
            elif response.status_code == 403:
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(5, 10))
                    
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(random.uniform(1, 3))
    
    return None

def get_sitemap_urls():
    """Парсинг Sitemap с ограничением в тестовом режиме"""
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

    # Фильтруем только movies-sitemap
    movie_sitemaps = [s for s in sitemaps if 'movies-sitemap' in s['loc']]
    logger.info(f"Найдено {len(movie_sitemaps)} movies-sitemap файлов")
    
    # В тестовом режиме ограничиваем количество
    if TEST_MODE:
        movie_sitemaps = movie_sitemaps[:TEST_SITEMAP_LIMIT]
        logger.info(f"ТЕСТ: обрабатываем только {len(movie_sitemaps)} sitemap(ов)")
    
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
                        
                        # В тестовом режиме останавливаемся при достижении лимита
                        if TEST_MODE and len(all_film_paths) >= TEST_FILM_LIMIT:
                            break
            
            logger.info(f"  -> Получено URL: {len(all_film_paths)}")
            
            if TEST_MODE and len(all_film_paths) >= TEST_FILM_LIMIT:
                break
                
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    if TEST_MODE:
        all_film_paths = all_film_paths[:TEST_FILM_LIMIT]
    
    logger.info(f"Итого URL для парсинга: {len(all_film_paths)}")
    return all_film_paths

def parse_film_page(scraper, url_path):
    """Парсинг страницы фильма"""
    full_url = urljoin(BASE_URL, url_path)
    
    resp = fetch_with_retry(scraper, full_url)
    if not resp:
        return None
    
    # Фикс кодировки
    if resp.encoding == 'ISO-8859-1':
        resp.encoding = 'utf-8'
    
    soup = BeautifulSoup(resp.text, 'html.parser')
    film_data = {}
    
    # Код фильма
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

    # Дата
    date_added = None
    if movie_table:
        for row in movie_table.find_all(['p', 'div']):
            text = row.get_text(strip=True)
            if 'Release Date:' in text:
                date_str = text.split('Release Date:')[-1].strip()
                try:
                    dt = datetime.strptime(date_str, '%Y-%m-%d')
                    date_added = dt.replace(tzinfo=timezone.utc).isoformat()
                except:
                    pass
    film_data['dateAdded'] = date_added or datetime.now(timezone.utc).isoformat()

    return film_data

def main():
    start_time = time.time()
    logger.info("="*60)
    logger.info("Парсер JAVDatabase")
    if TEST_MODE:
        logger.info(f"ТЕСТ: {TEST_SITEMAP_LIMIT} sitemap, {TEST_FILM_LIMIT} фильмов")
    logger.info("="*60)
    
    # Загружаем существующие
    existing_data = load_encrypted_json(ENCRYPTED_FILE, XOR_KEY)
    existing_films = existing_data.get('films', []) if existing_data else []
    existing_codes = {f['code'] for f in existing_films}
    logger.info(f"В базе: {len(existing_films)} фильмов")
    
    # Получаем URL
    film_paths = get_sitemap_urls()
    
    if not film_paths:
        logger.warning("Нет URL")
        return
    
    # Фильтруем новые
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
            logger.info(f"    Жанры: {', '.join(film['metadata']['genre'][:3])}")
            logger.info(f    Актрисы: {', '.join(film['metadata']['actress'][:3])}")
            logger.info(f"    Обложка: {film['thumbnail']}")
            logger.info(f"    Скриншотов: {len(film['screenshots'])}")
        else:
            logger.warning(f"  ✗ Ошибка")
        
        time.sleep(random.uniform(1.5, 3))
    
    # Сохраняем
    all_films = existing_films + new_films
    output = {
        "films": all_films,
        "metadata": {
            "version": "1.0.0",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "javdatabase.com",
            "totalFilms": len(all_films)
        }
    }
    
    save_encrypted_json(output, ENCRYPTED_FILE, XOR_KEY)
    
    # В тесте сохраняем читаемый JSON
    if TEST_MODE:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Готово! Новых: {len(new_films)}, всего: {len(all_films)}")
    logger.info(f"Время: {(time.time()-start_time)/60:.1f} мин")

if __name__ == "__main__":
    main()
