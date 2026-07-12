import cloudscraper
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import json
import time
import os
import queue
import subprocess
import requests
import gzip
import msgpack
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone, timedelta
import logging
import sys
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from threading import Lock

os.chdir(os.path.dirname(os.path.abspath(__file__)) + '/..')

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
ACTRESS_DATA_DIR = "actress_data"
ACTRESS_FILE = os.path.join(ACTRESS_DATA_DIR, "actress.bin")

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
actress_lock = Lock()

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

# --- Маппинг ключей для актрис (без age и debut_age) ---
ACTRESS_NEW_KEYS = {
    'name': 'n',
    'jp_name': 'jn',
    'dob': 'd',
    'debut': 'db',
    'birthplace': 'bp',
    'sign': 's',
    'blood': 'b',
    'measurements': 'ms',
    'cup': 'c',
    'height': 'h',
    'shoe_size': 'ss',
    'hair_length': 'hl',
    'hair_color': 'hc',
    'tags': 't',
    'url': 'u',
    'lastmod': 'lm'
}

ACTRESS_OLD_KEYS = {v: k for k, v in ACTRESS_NEW_KEYS.items()}

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

def minify_actress_json(data):
    """Минифицирует JSON актрисы"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            new_key = ACTRESS_NEW_KEYS.get(k, k)
            if isinstance(v, (dict, list)):
                result[new_key] = minify_actress_json(v)
            else:
                result[new_key] = v
        return result
    elif isinstance(data, list):
        return [minify_actress_json(item) for item in data]
    return data

def normalize_json(data):
    """Нормализует JSON после загрузки (поддерживает и старые и новые ключи)"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in OLD_KEYS:
                result[OLD_KEYS[k]] = normalize_json(v)
            else:
                result[k] = normalize_json(v)
        return result
    elif isinstance(data, list):
        return [normalize_json(item) for item in data]
    return data

def normalize_actress_json(data):
    """Нормализует JSON актрисы после загрузки"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in ACTRESS_OLD_KEYS:
                result[ACTRESS_OLD_KEYS[k]] = normalize_actress_json(v)
            else:
                result[k] = normalize_actress_json(v)
        return result
    elif isinstance(data, list):
        return [normalize_actress_json(item) for item in data]
    return data

def parse_datetime(date_str):
    """Парсит дату из различных форматов"""
    if not date_str:
        return None
    
    formats = [
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S+00:00',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%d %H:%M:%S',
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    
    try:
        return datetime.fromisoformat(date_str)
    except:
        return None

# --- Шифрование ---

def xor_encrypt_decrypt(data: bytes, key: str) -> bytes:
    key_bytes = key.encode('utf-8')
    result = bytearray(len(data))
    for i in range(len(data)):
        result[i] = data[i] ^ key_bytes[i % len(key_bytes)]
    return bytes(result)

def save_encrypted(data: dict, filepath: str, key: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    minified = minify_json(data)
    msgpack_bytes = msgpack.packb(minified)
    compressed = gzip.compress(msgpack_bytes, compresslevel=9)
    encrypted = xor_encrypt_decrypt(compressed, key)
    with open(filepath, 'wb') as f:
        f.write(encrypted)

def save_actress_encrypted(data: dict, filepath: str, key: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    minified = minify_actress_json(data)
    msgpack_bytes = msgpack.packb(minified)
    compressed = gzip.compress(msgpack_bytes, compresslevel=9)
    encrypted = xor_encrypt_decrypt(compressed, key)
    with open(filepath, 'wb') as f:
        f.write(encrypted)
    
    # Коммитим после сохранения актрис
    commit_actress_data()

def load_encrypted(filepath: str, key: str) -> dict:
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        encrypted = f.read()
    compressed = xor_encrypt_decrypt(encrypted, key)
    msgpack_bytes = gzip.decompress(compressed)
    data = msgpack.unpackb(msgpack_bytes)
    return normalize_json(data)

def load_actress_encrypted(filepath: str, key: str) -> dict:
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        encrypted = f.read()
    compressed = xor_encrypt_decrypt(encrypted, key)
    msgpack_bytes = gzip.decompress(compressed)
    data = msgpack.unpackb(msgpack_bytes)
    return normalize_actress_json(data)

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

# --- Парсинг актрисы (без age и debut_age) ---

def parse_actress_page(url_path, lastmod=None):
    """Парсит страницу актрисы"""
    for attempt in range(MAX_RETRIES):
        scraper = get_scraper()
        try:
            full_url = urljoin(BASE_URL, url_path)
            time.sleep(random.uniform(0.5, 1.5))
            
            resp = fetch_with_retry(scraper, full_url)
            if not resp:
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"    Попытка {attempt+1} не удалась")
                    continue
                return None
            
            if resp.encoding == 'ISO-8859-1':
                resp.encoding = 'utf-8'
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Имя
            name = None
            jp_name = None
            
            h1 = soup.find('h1', class_='idol-name')
            if h1:
                name_text = h1.get_text(strip=True)
                # Убираем " - JAV Profile"
                name = name_text.replace(' - JAV Profile', '').strip()
            
            # Японское имя
            jp_elem = soup.find('b', string='JP:')
            if jp_elem and jp_elem.next_sibling:
                jp_name = jp_elem.next_sibling.strip()
            
            if not name:
                path_parts = url_path.strip('/').split('/')
                if len(path_parts) >= 2 and path_parts[-2] == 'idols':
                    raw_name = path_parts[-1].replace('-', ' ')
                    name = raw_name.title()
            
            # Дата рождения
            dob = None
            dob_b = soup.find('b', string='DOB:')
            if dob_b:
                dob_link = dob_b.find_next('a', class_='idol-box-link')
                if dob_link:
                    dob_text = dob_link.get_text(strip=True)
                    if dob_text and '?' not in dob_text:
                        dob = dob_text
            
            # Дебют
            debut = None
            debut_b = soup.find('b', string='Debut:')
            if debut_b:
                debut_link = debut_b.find_next('a', class_='idol-box-link')
                if debut_link:
                    debut_text = debut_link.get_text(strip=True)
                    if debut_text and '?' not in debut_text:
                        debut = debut_text
            
            # Место рождения
            birthplace = None
            bp_b = soup.find('b', string='Birthplace:')
            if bp_b:
                bp_text = bp_b.next_sibling
                if bp_text:
                    bp_text = bp_text.strip()
                    if bp_text and '?' not in bp_text and '-' not in bp_text:
                        birthplace = bp_text
            
            # Знак зодиака
            sign = None
            sign_b = soup.find('b', string='Sign:')
            if sign_b:
                sign_text = sign_b.next_sibling
                if sign_text:
                    sign_text = sign_text.strip()
                    if sign_text and '?' not in sign_text and '-' not in sign_text:
                        sign = sign_text
            
            # Группа крови
            blood = None
            blood_b = soup.find('b', string='Blood:')
            if blood_b:
                blood_text = blood_b.next_sibling
                if blood_text:
                    blood_text = blood_text.strip()
                    if blood_text and '?' not in blood_text and '-' not in blood_text:
                        blood = blood_text
            
            # Размеры
            measurements = None
            ms_b = soup.find('b', string='Measurements:')
            if ms_b:
                ms_text = ms_b.next_sibling
                if ms_text:
                    ms_text = ms_text.strip()
                    if ms_text and '?' not in ms_text and '-' not in ms_text:
                        measurements = ms_text
            
            # Размер чашки
            cup = None
            cup_b = soup.find('b', string='Cup:')
            if cup_b:
                cup_link = cup_b.find_next('a', class_='idol-box-link')
                if cup_link:
                    cup_text = cup_link.get_text(strip=True)
                    if cup_text:
                        cup = cup_text
            
            # Рост
            height = None
            height_b = soup.find('b', string='Height:')
            if height_b:
                height_link = height_b.find_next('a', class_='idol-box-link')
                if height_link:
                    height_text = height_link.get_text(strip=True)
                    if height_text:
                        height = height_text
            
            # Размер обуви
            shoe_size = None
            ss_b = soup.find('b', string='Shoe Size:')
            if ss_b:
                ss_text = ss_b.next_sibling
                if ss_text:
                    ss_text = ss_text.strip()
                    if ss_text and '?' not in ss_text and '-' not in ss_text:
                        shoe_size = ss_text
            
            # Длина волос
            hair_length = []
            hl_b = soup.find('b', string='Hair Length(s):')
            if hl_b:
                for link in hl_b.find_next_siblings('a', class_='idol-box-link'):
                    hl_text = link.get_text(strip=True)
                    if hl_text:
                        hair_length.append(hl_text)
            
            # Цвет волос
            hair_color = []
            hc_b = soup.find('b', string='Hair Color(s):')
            if hc_b:
                for link in hc_b.find_next_siblings('a', class_='idol-box-link'):
                    hc_text = link.get_text(strip=True)
                    if hc_text:
                        hair_color.append(hc_text)
            
            # Теги
            tags = []
            tags_b = soup.find('b', string='Tags:')
            if tags_b:
                for link in tags_b.find_next_siblings('a', class_='idol-box-link'):
                    tag_text = link.get_text(strip=True)
                    if tag_text:
                        tags.append(tag_text)
            
            actress_data = {
                'name': name,
                'jp_name': jp_name,
                'dob': dob,
                'debut': debut,
                'birthplace': birthplace,
                'sign': sign,
                'blood': blood,
                'measurements': measurements,
                'cup': cup,
                'height': height,
                'shoe_size': shoe_size,
                'hair_length': hair_length,
                'hair_color': hair_color,
                'tags': tags,
                'url': url_path,
                'lastmod': lastmod
            }
            
            return_scraper(scraper)
            return actress_data
            
        except Exception as e:
            logger.error(f"    Ошибка парсинга актрисы: {e}")
            if attempt < MAX_RETRIES - 1:
                continue
            return None

# --- Коммит данных актрис ---

def commit_actress_data():
    """Коммитит только данные актрис"""
    try:
        # Настраиваем git config
        subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], 
                      check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'GitHub Actions'], 
                      check=True, capture_output=True)
        
        # Добавляем только файлы актрис
        subprocess.run(['git', 'add', 'actress_data/'], 
                      check=True, capture_output=True)
        
        # Проверяем, есть ли изменения
        result = subprocess.run(['git', 'diff', '--staged', '--quiet'], capture_output=True)
        if result.returncode != 0:
            # Есть изменения - коммитим
            subprocess.run(['git', 'commit', '-m', 
                          f'Update actresses {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}'], 
                          check=True, capture_output=True)
            
            # Пробуем запушить
            try:
                # Сначала пробуем просто push
                push_result = subprocess.run(['git', 'push'], 
                                           capture_output=True, text=True)
                
                if push_result.returncode != 0:
                    # Если push не удался, пробуем pull с rebase
                    logger.info("  Push не удался, пробуем pull --rebase")
                    
                    # Сохраняем текущие изменения
                    subprocess.run(['git', 'fetch', 'origin'], 
                                 check=True, capture_output=True)
                    
                    # Пробуем rebase
                    rebase_result = subprocess.run(['git', 'pull', '--rebase'], 
                                                 capture_output=True, text=True)
                    
                    if rebase_result.returncode != 0:
                        # Если rebase не удался, отменяем его
                        logger.warning(f"  Rebase не удался: {rebase_result.stderr}")
                        subprocess.run(['git', 'rebase', '--abort'], 
                                     capture_output=True)
                        
                        # Пробуем pull с merge вместо rebase
                        merge_result = subprocess.run(['git', 'pull', '--no-rebase'], 
                                                    capture_output=True, text=True)
                        
                        if merge_result.returncode != 0:
                            logger.warning(f"  Merge тоже не удался: {merge_result.stderr}")
                            # В крайнем случае, пробуем force push
                            logger.info("  Пробуем force push")
                            subprocess.run(['git', 'push', '--force-with-lease'], 
                                         check=True, capture_output=True)
                        else:
                            # Merge удался, пушим
                            subprocess.run(['git', 'push'], 
                                         check=True, capture_output=True)
                    else:
                        # Rebase удался, пушим
                        subprocess.run(['git', 'push'], 
                                     check=True, capture_output=True)
                
                logger.info("  📤 Данные актрис закоммичены и запушены")
                
            except subprocess.CalledProcessError as e:
                logger.warning(f"  Push/pull не удался: {e}")
                # В крайнем случае - force push
                try:
                    subprocess.run(['git', 'push', '--force-with-lease'], 
                                 check=True, capture_output=True)
                    logger.info("  📤 Данные актрис запушены с --force-with-lease")
                except:
                    logger.error(f"  Не удалось запушить даже с --force-with-lease")
        else:
            logger.info("  Нет изменений для коммита")
            
    except Exception as e:
        logger.error(f"  Ошибка коммита актрис: {e}")

# --- Обработка sitemap актрис ---

def process_actress_sitemap(sitemap_url, key, cache):
    """Обрабатывает sitemap актрис"""
    
    logger.info(f"  Загрузка sitemap актрис: {sitemap_url}")
    
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
            lastmod = url.find('sm:lastmod', ns)
            
            if loc is not None and '/idols/' in loc.text:
                lastmod_text = lastmod.text if lastmod is not None else None
                
                # Проверяем, нужно ли парсить
                actress_url = loc.text
                if actress_url in cache.get('processed_actresses', {}):
                    cached_time_str = cache['processed_actresses'][actress_url]
                    if lastmod_text and cached_time_str:
                        sitemap_time = parse_datetime(lastmod_text)
                        cached_time = parse_datetime(cached_time_str)
                        
                        if sitemap_time and cached_time and sitemap_time <= cached_time:
                            continue  # Пропускаем, не изменилось
                
                urls.append((loc.text, lastmod_text))
        
        logger.info(f"  Новых/обновленных актрис: {len(urls)}")
        
        if not urls:
            return 0
        
        # Загружаем существующие данные актрис
        existing_actresses = {}
        actress_data_file = load_actress_encrypted(ACTRESS_FILE, key)
        if actress_data_file:
            existing_actresses = actress_data_file.get('actresses', {})
        
        parsed_count = 0
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_url = {
                executor.submit(parse_actress_page, urlparse(full_url).path, lastmod): full_url 
                for full_url, lastmod in urls
            }
            
            for future in as_completed(future_to_url):
                full_url = future_to_url[future]
                
                try:
                    actress = future.result(timeout=60)
                    
                    if actress and actress['name']:
                        # Используем имя как ключ
                        name_key = actress['name'].lower()
                        
                        with actress_lock:
                            existing_actresses[name_key] = actress
                            parsed_count += 1
                        
                        logger.info(f"    ✓ {actress['name']}")
                    else:
                        logger.warning(f"    ✗ {full_url}")
                        
                except FuturesTimeoutError:
                    logger.error(f"    ⏰ Таймаут 60с: {full_url}")
                    future.cancel()
                except Exception as e:
                    logger.error(f"    ✗ {full_url}: {e}")
        
        # Сохраняем актрис
        if parsed_count > 0:
            actress_data = {
                'actresses': existing_actresses,
                'metadata': {
                    'version': '1.0.0',
                    'generatedAt': datetime.now(timezone.utc).isoformat(),
                    'totalActresses': len(existing_actresses)
                }
            }
            save_actress_encrypted(actress_data, ACTRESS_FILE, key)
            logger.info(f"  💾 Сохранено {parsed_count} актрис (всего {len(existing_actresses)})")
        
        # Обновляем кэш
        with cache_lock:
            if 'processed_actresses' not in cache:
                cache['processed_actresses'] = {}
            for full_url, _ in urls:
                cache['processed_actresses'][full_url] = datetime.now(timezone.utc).isoformat()
            
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
        
        return parsed_count
        
    except Exception as e:
        logger.error(f"  Ошибка обработки sitemap актрис: {e}")
        return 0

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
    """Обрабатывает sitemap только если он изменился (по lastmod)"""
    
    sitemap_lastmod = cache.get('sitemap_index_data', {}).get(sitemap_url)
    
    if sitemap_url in cache.get('processed', {}):
        cached_time_str = cache['processed'][sitemap_url]
        if sitemap_lastmod and cached_time_str:
            sitemap_time = parse_datetime(sitemap_lastmod)
            cached_time = parse_datetime(cached_time_str)
            
            if sitemap_time and cached_time:
                if sitemap_time <= cached_time:
                    logger.info(f"  Пропуск (не изменился): {sitemap_url}")
                    logger.info(f"    lastmod: {sitemap_lastmod}, кэш: {cached_time_str}")
                    return 0
    
    logger.info(f"  Загрузка: {sitemap_url}")
    if sitemap_lastmod:
        logger.info(f"    lastmod: {sitemap_lastmod}")
    
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
                if 'processed' not in cache:
                    cache['processed'] = {}
                cache['processed'][sitemap_url] = datetime.now(timezone.utc).isoformat()
            return 0
        
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
        
        if films_by_month:
            save_month_batch(films_by_month, key)
        
        with cache_lock:
            if 'processed' not in cache:
                cache['processed'] = {}
            cache['processed'][sitemap_url] = datetime.now(timezone.utc).isoformat()
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
        
        with open(METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "lastUpdate": datetime.now(timezone.utc).isoformat(),
                "totalFilms": len(existing_codes)
            }, f, ensure_ascii=False, indent=2)
        
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
        
        subprocess.run(['git', 'add', 'data/', 'metadata.json', 
                       'sitemap_cache.json'], 
                      check=True, capture_output=True)
        result = subprocess.run(['git', 'diff', '--staged', '--quiet'], capture_output=True)
        if result.returncode != 0:
            subprocess.run(['git', 'commit', '-m', 
                          f'Update {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}'], 
                          check=True, capture_output=True)
            
            # Пробуем push с fallback-стратегией
            try:
                push_result = subprocess.run(['git', 'push'], 
                                           capture_output=True, text=True)
                
                if push_result.returncode != 0:
                    logger.info("  Push не удался, пробуем pull --rebase")
                    subprocess.run(['git', 'fetch', 'origin'], check=True, capture_output=True)
                    
                    rebase_result = subprocess.run(['git', 'pull', '--rebase'], 
                                                 capture_output=True, text=True)
                    
                    if rebase_result.returncode != 0:
                        logger.warning(f"  Rebase не удался: {rebase_result.stderr}")
                        subprocess.run(['git', 'rebase', '--abort'], capture_output=True)
                        
                        merge_result = subprocess.run(['git', 'pull', '--no-rebase'], 
                                                    capture_output=True, text=True)
                        
                        if merge_result.returncode != 0:
                            logger.warning(f"  Merge не удался: {merge_result.stderr}")
                            subprocess.run(['git', 'push', '--force-with-lease'], 
                                         check=True, capture_output=True)
                        else:
                            subprocess.run(['git', 'push'], check=True, capture_output=True)
                    else:
                        subprocess.run(['git', 'push'], check=True, capture_output=True)
                
                logger.info("  📤 Закоммичено и запушено")
                
            except subprocess.CalledProcessError as e:
                logger.warning(f"  Push/pull не удался: {e}")
                try:
                    subprocess.run(['git', 'push', '--force-with-lease'], 
                                 check=True, capture_output=True)
                    logger.info("  📤 Запушено с --force-with-lease")
                except:
                    logger.error(f"  Не удалось запушить даже с --force-with-lease")
    except Exception as e:
        logger.error(f"  Ошибка коммита: {e}")

# --- Главная ---

def main():
    start_time = time.time()
    logger.info("="*60)
    logger.info(f"Парсер JAVDatabase (workers={MAX_WORKERS}, timeout=60s)")
    logger.info("="*60)
    
    init_scraper_pool(POOL_SIZE)
    
    # Загружаем sitemap index для получения ВСЕХ sitemap (и фильмы, и актрисы)
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
    
    # Разделяем sitemap на фильмы и актрис
    movie_sitemaps = {}
    actress_sitemaps = {}
    sitemap_index_data = {}
    
    for sitemap in root.findall('sm:sitemap', ns):
        loc = sitemap.find('sm:loc', ns)
        lastmod = sitemap.find('sm:lastmod', ns)
        
        if loc is not None:
            lastmod_text = lastmod.text if lastmod is not None else None
            sitemap_index_data[loc.text] = lastmod_text
            
            if 'movies-sitemap' in loc.text:
                movie_sitemaps[loc.text] = lastmod_text
            elif 'idols-sitemap' in loc.text:
                actress_sitemaps[loc.text] = lastmod_text
    
    logger.info(f"Найдено: {len(movie_sitemaps)} movies-sitemap, {len(actress_sitemaps)} idols-sitemap")
    
    # Загружаем общий кэш
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            logger.info(f"Кэш загружен")
        except:
            cache = {'processed': {}, 'processed_actresses': {}, 'sitemap_index_data': {}}
    else:
        cache = {'processed': {}, 'processed_actresses': {}, 'sitemap_index_data': {}}
    
    cache['sitemap_index_data'] = sitemap_index_data
    
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)
    
    # --- Парсинг актрис ---
    logger.info("\n" + "="*60)
    logger.info("ПАРСИНГ АКТРИС")
    logger.info("="*60)
    
    total_actresses = 0
    for i, (sitemap_url, lastmod) in enumerate(actress_sitemaps.items(), 1):
        logger.info(f"[{i}/{len(actress_sitemaps)}] {sitemap_url}")
        if lastmod:
            logger.info(f"  lastmod: {lastmod}")
        
        parsed = process_actress_sitemap(sitemap_url, XOR_KEY, cache)
        total_actresses += parsed
    
    logger.info(f"Всего обработано актрис: {total_actresses}")
    
    # --- Парсинг фильмов ---
    logger.info("\n" + "="*60)
    logger.info("ПАРСИНГ ФИЛЬМОВ")
    logger.info("="*60)
    
    existing_codes = load_existing_codes(XOR_KEY)
    logger.info(f"В базе: {len(existing_codes)} фильмов")
    
    total_parsed = 0
    skipped = 0
    processed = 0
    
    for i, (sitemap_url, lastmod) in enumerate(movie_sitemaps.items(), 1):
        logger.info(f"[{i}/{len(movie_sitemaps)}] {sitemap_url}")
        if lastmod:
            logger.info(f"  lastmod: {lastmod}")
        
        parsed = process_sitemap(sitemap_url, existing_codes, XOR_KEY, cache)
        if parsed == 0:
            if sitemap_url in cache.get('processed', {}):
                skipped += 1
        else:
            processed += 1
            total_parsed += parsed
    
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "lastUpdate": datetime.now(timezone.utc).isoformat(),
            "totalFilms": len(existing_codes),
            "totalActresses": total_actresses
        }, f, ensure_ascii=False, indent=2)
    
    commit_and_push()
    
    logger.info("="*60)
    logger.info(f"Готово! Фильмов: {len(existing_codes)}")
    logger.info(f"Актрис обработано: {total_actresses}")
    logger.info(f"Обработано sitemap'ов фильмов: {processed}, пропущено: {skipped}")
    logger.info(f"Новых фильмов: {total_parsed}")
    logger.info(f"Время: {(time.time()-start_time)/60:.1f} мин")
    logger.info("="*60)

if __name__ == "__main__":
    main()