import cloudscraper
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import json
import time
import os
import queue
import subprocess
import requests
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
import logging
import sys
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from threading import Lock

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

MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '2'))
POOL_SIZE = MAX_WORKERS + 2

XOR_KEY = os.environ.get('XOR_KEY', 'local_dev_fallback_key_change_me')

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
]

REQUEST_TIMEOUT = 120
MAX_RETRIES = 2

# Блокировки
existing_codes_lock = Lock()
cache_lock = Lock()

# Пул scraper'ов
scraper_pool = queue.Queue(maxsize=POOL_SIZE)

# --- Маппинг для новых сокращенных ключей ---
NEW_KEYS = {
    'code': 'c',
    'title': 't',
    'thumbnail': 'th',
    'screenshots': 'ss',
    'metadata': 'mt',
    'genre': 'g',
    'actress': 'a',
    'releaseDate': 'rd',
    'version': 'v',
    'generatedAt': 'ga',
    'month': 'm',
    'totalFilms': 'tf',
    'films': 'f'
}

# Ключи, которые могут быть в старых данных
OLD_KEYS = {
    'c': 'code',
    't': 'title',
    'd': 'description',
    'th': 'thumbnail',
    'ss': 'screenshots',
    'mt': 'metadata',
    'g': 'genre',
    'a': 'actress',
    'rd': 'releaseDate',
    'v': 'version',
    'ga': 'generatedAt',
    'm': 'month',
    'tf': 'totalFilms',
    'f': 'films'
}

def minify_json(data):
    """Минифицирует JSON для сохранения (без description)"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            # Пропускаем description
            if k == 'description':
                continue
            # Используем новые ключи
            new_key = NEW_KEYS.get(k, k)
            result[new_key] = minify_json(v)
        return result
    elif isinstance(data, list):
        return [minify_json(item) for item in data]
    return data

def normalize_json(data):
    """Нормализует JSON после загрузки (поддерживает и старые и новые ключи)"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            # Пробуем найти в старых сокращенных ключах
            if k in OLD_KEYS:
                result[OLD_KEYS[k]] = normalize_json(v)
            else:
                # Или оставляем как есть (для полных ключей из старых данных)
                result[k] = normalize_json(v)
        return result
    elif isinstance(data, list):
        return [normalize_json(item) for item in data]
    return data

# --- Шифрование ---

def xor_encrypt_decrypt(data: bytes, key: str) -> bytes:
    key_bytes = key.encode('utf-8')
    result = bytearray(len(data))
    for i in range(len(data)):
        result[i] = data[i] ^ key_bytes[i % len(key_bytes)]
    return bytes(result)

def save_encrypted(data: dict, filepath: str, key: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    # Минифицируем перед сохранением
    minified = minify_json(data)
    # Компактный JSON
    json_str = json.dumps(minified, ensure_ascii=False, separators=(',', ':'))
    encrypted = xor_encrypt_decrypt(json_str.encode('utf-8'), key)
    with open(filepath, 'wb') as f:
        f.write(encrypted)

def load_encrypted(filepath: str, key: str) -> dict:
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        encrypted = f.read()
    decrypted = xor_encrypt_decrypt(encrypted, key)
    data = json.loads(decrypted.decode('utf-8'))
    # Нормализуем (поддерживает старые и новые форматы)
    return normalize_json(data)

# --- Scraper pool ---

def create_scraper():
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
        delay=5
    )
    scraper.timeout = REQUEST_TIMEOUT
    scraper.headers.update({
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
    })
    return scraper

def init_scraper_pool(size):
    for _ in range(size):
        scraper_pool.put(create_scraper())
    logger.info(f"Пул scraper'ов: {size}")

def get_scraper():
    try:
        return scraper_pool.get(timeout=10)
    except queue.Empty:
        logger.warning("Пул пуст, создаю новый scraper")
        return create_scraper()

def return_scraper(scraper):
    if scraper is None:
        return
    try:
        if hasattr(scraper, 'get') and hasattr(scraper, 'headers'):
            scraper_pool.put_nowait(scraper)
    except queue.Full:
        pass

def fetch_with_retry(scraper, url, max_retries=MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                scraper.headers.update({'User-Agent': random.choice(USER_AGENTS)})
                time.sleep(random.uniform(0.5, 1.5))
            
            response = scraper.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                if response.encoding == 'ISO-8859-1':
                    response.encoding = 'utf-8'
                return response
            elif response.status_code == 403:
                time.sleep(random.uniform(5, 10))
            elif response.status_code == 429:
                time.sleep(random.uniform(10, 20))
                    
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(random.uniform(1, 3))
    
    return None

# --- Кэш и сохранение ---

def load_existing_codes(key):
    codes = set()
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.endswith('.bin'):
                data = load_encrypted(os.path.join(DATA_DIR, f), key)
                if data:
                    for film in data.get('films', []):
                        codes.add(film['code'])
    return codes

def save_month_batch(month_films_dict, key):
    total = 0
    for month, films in month_films_dict.items():
        filepath = os.path.join(DATA_DIR, f"{month}.bin")
        
        existing = load_encrypted(filepath, key)
        existing_films = existing.get('films', []) if existing else []
        
        codes = {f['code'] for f in existing_films}
        new = [f for f in films if f['code'] not in codes]
        
        if not new:
            continue
        
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
        saved = len(new)
        total += saved
        logger.info(f"  💾 {month}.bin: +{saved} фильмов (всего {len(all_films)})")
    
    return total

# --- Парсинг фильма (БЕЗ description) ---

def parse_film_page(url_path):
    for attempt in range(MAX_RETRIES):
        scraper = get_scraper()
        try:
            full_url = urljoin(BASE_URL, url_path)
            time.sleep(random.uniform(0.5, 1.5))
            
            resp = fetch_with_retry(scraper, full_url)
            if not resp:
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"    Попытка {attempt+1} не удалась, новый scraper")
                    continue
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

            # Обложка
            thumb = None
            poster = soup.find('div', id='poster-container')
            if poster:
                img = poster.find('img')
                if img and img.get('src'):
                    thumb = urlparse(urljoin(BASE_URL, img['src'])).path
            
            if not thumb:
                og_img = soup.find('meta', property='og:image')
                if og_img and og_img.get('content'):
                    thumb = urlparse(og_img['content']).path
            
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

            return_scraper(scraper)
            return film_data
            
        except Exception as e:
            logger.error(f"    Ошибка: {e}")
            if attempt < MAX_RETRIES - 1:
                continue
            return None

# --- Обработка sitemap ---

def process_sitemap(sitemap_url, existing_codes, key, cache):
    if sitemap_url in cache:
        logger.info(f"  Пропуск (в кэше): {sitemap_url}")
        return 0
    
    logger.info(f"  Загрузка: {sitemap_url}")
    
    scraper = get_scraper()
    try:
        time.sleep(random.uniform(1, 2))
        resp = fetch_with_retry(scraper, sitemap_url)
    finally:
        return_scraper(scraper)
    
    if not resp:
        return 0
    
    try:
        root = ET.fromstring(resp.content)
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        
        urls = []
        for url in root.findall('sm:url', ns):
            loc = url.find('sm:loc', ns)
            if loc is not None:
                parsed = urlparse(loc.text)
                if '/movies/' in parsed.path:
                    code = parsed.path.strip('/').split('/')[-1].upper()
                    with existing_codes_lock:
                        if code not in existing_codes:
                            urls.append(parsed.path)
        
        logger.info(f"  Новых URL: {len(urls)}")
        
        if not urls:
            with cache_lock:
                cache[sitemap_url] = datetime.now(timezone.utc).isoformat()
            return 0
        
        # Многопоточный парсинг
        parsed_count = 0
        films_by_month = defaultdict(list)
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_url = {executor.submit(parse_film_page, path): path for path in urls}
            
            for future in as_completed(future_to_url):
                path = future_to_url[future]
                code = path.strip('/').split('/')[-1].upper()
                
                try:
                    film = future.result(timeout=60)
                    
                    if film:
                        month = film['releaseDate'][:7]
                        
                        with existing_codes_lock:
                            existing_codes.add(code)
                            films_by_month[month].append(film)
                            parsed_count += 1
                        
                        logger.info(f"    ✓ {film['code']}: {film['title'][:50]}")
                    else:
                        logger.warning(f"    ✗ {code}")
                        
                except FuturesTimeoutError:
                    logger.error(f"    ⏰ Таймаут 60с: {code}")
                    future.cancel()
                except Exception as e:
                    logger.error(f"    ✗ {code}: {e}")
        
        # Сохраняем
        if films_by_month:
            save_month_batch(films_by_month, key)
        
        # Кэш
        with cache_lock:
            cache[sitemap_url] = datetime.now(timezone.utc).isoformat()
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
        
        # Метаданные
        with open(METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "lastUpdate": datetime.now(timezone.utc).isoformat(),
                "totalFilms": len(existing_codes)
            }, f, ensure_ascii=False, indent=2)
        
        # Коммит
        commit_and_push()
        
        return parsed_count
        
    except Exception as e:
        logger.error(f"  Ошибка: {e}")
        return 0

def commit_and_push():
    try:
        subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], 
                      check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'GitHub Actions'], 
                      check=True, capture_output=True)
        
        subprocess.run(['git', 'add', 'data/', 'metadata.json', 'sitemap_cache.json'], 
                      check=True, capture_output=True)
        result = subprocess.run(['git', 'diff', '--staged', '--quiet'], capture_output=True)
        if result.returncode != 0:
            subprocess.run(['git', 'commit', '-m', 
                          f'Update {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}'], 
                          check=True, capture_output=True)
            subprocess.run(['git', 'pull', '--rebase'], check=True, capture_output=True)
            subprocess.run(['git', 'push'], check=True, capture_output=True)
            logger.info("  📤 Закоммичено и запушено")
    except Exception as e:
        logger.error(f"  Ошибка коммита: {e}")

# --- Главная ---

def main():
    start_time = time.time()
    logger.info("="*60)
    logger.info(f"Парсер JAVDatabase (workers={MAX_WORKERS}, timeout=60s)")
    logger.info("="*60)
    
    init_scraper_pool(POOL_SIZE)
    
    scraper = get_scraper()
    try:
        logger.info(f"Загрузка sitemap-индекса: {SITEMAP_INDEX_URL}")
        resp = fetch_with_retry(scraper, SITEMAP_INDEX_URL)
    finally:
        return_scraper(scraper)
    
    if not resp:
        logger.error("Не удалось получить sitemap index")
        return
    
    root = ET.fromstring(resp.content)
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    
    movie_sitemaps = []
    for sitemap in root.findall('sm:sitemap', ns):
        loc = sitemap.find('sm:loc', ns)
        if loc is not None and 'movies-sitemap' in loc.text:
            movie_sitemaps.append(loc.text)
    
    logger.info(f"Найдено {len(movie_sitemaps)} movies-sitemap файлов")
    
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            logger.info(f"Кэш: {len(cache)} sitemap'ов")
        except:
            pass
    
    existing_codes = load_existing_codes(XOR_KEY)
    logger.info(f"В базе: {len(existing_codes)} фильмов")
    
    total_parsed = 0
    for i, sitemap_url in enumerate(movie_sitemaps, 1):
        logger.info(f"[{i}/{len(movie_sitemaps)}]")
        
        parsed = process_sitemap(sitemap_url, existing_codes, XOR_KEY, cache)
        total_parsed += parsed
    
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "lastUpdate": datetime.now(timezone.utc).isoformat(),
            "totalFilms": len(existing_codes)
        }, f, ensure_ascii=False, indent=2)
    
    commit_and_push()
    
    logger.info("="*60)
    logger.info(f"Готово! Фильмов: {len(existing_codes)}")
    logger.info(f"Время: {(time.time()-start_time)/60:.1f} мин")
    logger.info("="*60)

if __name__ == "__main__":
    main()
