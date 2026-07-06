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
import hashlib

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

# Тестовый режим - спарсить только N фильмов для проверки
TEST_MODE = os.environ.get('TEST_MODE', 'false').lower() == 'true'
TEST_LIMIT = int(os.environ.get('TEST_LIMIT', '5'))

# Ключ для XOR шифрования
XOR_KEY = os.environ.get('XOR_KEY', '299af363382d01e6ad36ddca7fa39ca92ee1627efe733dc6')

# User-Agent'ы
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
]

REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

# --- Функции шифрования ---

def xor_encrypt_decrypt(data: bytes, key: str) -> bytes:
    key_bytes = key.encode('utf-8')
    key_length = len(key_bytes)
    result = bytearray(len(data))
    for i in range(len(data)):
        result[i] = data[i] ^ key_bytes[i % key_length]
    return bytes(result)

def save_encrypted_json(data: dict, filepath: str, key: str):
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    json_bytes = json_str.encode('utf-8')
    encrypted = xor_encrypt_decrypt(json_bytes, key)
    encoded = base64.b64encode(encrypted)
    with open(filepath, 'wb') as f:
        f.write(encoded)

def load_encrypted_json(filepath: str, key: str) -> dict:
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        encoded = f.read()
    encrypted = base64.b64decode(encoded)
    decrypted = xor_encrypt_decrypt(encrypted, key)
    return json.loads(decrypted.decode('utf-8'))

# --- Основные функции ---

def create_scraper():
    """Создает cloudscraper"""
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
        delay=10
    )
    scraper.headers.update({
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
    })
    return scraper

def fetch_with_retry(scraper, url, max_retries=MAX_RETRIES):
    """Загружает URL с повторными попытками"""
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                scraper.headers.update({'User-Agent': random.choice(USER_AGENTS)})
                time.sleep(random.uniform(2, 5))
            
            response = scraper.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                # Пробуем разные кодировки
                try:
                    response.content.decode('utf-8')
                except:
                    # Если UTF-8 не работает, пробуем другие кодировки
                    for encoding in ['latin-1', 'cp1252', 'iso-8859-1']:
                        try:
                            response.content.decode(encoding)
                            response.encoding = encoding
                            break
                        except:
                            continue
                return response
            elif response.status_code == 403:
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(5, 10))
                continue
                
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(random.uniform(1, 3))
            continue
    
    return None

def get_sitemap_urls():
    """Парсинг Sitemap"""
    scraper = create_scraper()
    
    logger.info(f"Загрузка главного sitemap-индекса: {SITEMAP_INDEX_URL}")
    
    resp = fetch_with_retry(scraper, SITEMAP_INDEX_URL)
    if not resp:
        logger.error("Не удалось получить sitemap index")
        return []
    
    root = ET.fromstring(resp.content)
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    sitemaps = []
    
    for sitemap in root.findall('sm:sitemap', ns):
        loc_elem = sitemap.find('sm:loc', ns)
        lastmod_elem = sitemap.find('sm:lastmod', ns)
        if loc_elem is not None:
            sitemaps.append({
                'loc': loc_elem.text,
                'lastmod': lastmod_elem.text if lastmod_elem is not None else None
            })

    logger.info(f"Найдено {len(sitemaps)} файлов sitemap")
    
    # Загрузка кэша
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            logger.info(f"Кэш загружен. Записей: {len(cache)}")
        except:
            pass

    all_film_paths = set()
    new_cache = {}
    
    for i, sitemap_data in enumerate(sitemaps, 1):
        loc = sitemap_data['loc']
        lastmod = sitemap_data['lastmod']
        new_cache[loc] = lastmod

        if not ('movies-sitemap' in loc or 'post-sitemap' in loc):
            continue
        
        if loc in cache and cache[loc] == lastmod and lastmod is not None:
            continue
        
        logger.info(f"[{i}/{len(sitemaps)}] Обработка: {loc}")
        
        time.sleep(random.uniform(1, 2))
        resp = fetch_with_retry(scraper, loc)
        
        if not resp:
            logger.error(f"Не удалось загрузить {loc}")
            continue
        
        try:
            sitemap_root = ET.fromstring(resp.content)
            urls_found = 0
            
            for url in sitemap_root.findall('sm:url', ns):
                film_loc_elem = url.find('sm:loc', ns)
                if film_loc_elem is not None:
                    parsed_url = urlparse(film_loc_elem.text)
                    if '/movies/' in parsed_url.path:
                        all_film_paths.add(parsed_url.path)
                        urls_found += 1
            
            logger.info(f"  -> +{urls_found} URL (всего: {len(all_film_paths)})")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке {loc}: {e}")

    # Сохраняем кэш
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_cache, f, indent=2)
    
    logger.info(f"Всего URL фильмов: {len(all_film_paths)}")
    return list(all_film_paths)

def parse_film_page(scraper, url_path):
    """Парсинг страницы фильма с детальным логированием"""
    full_url = urljoin(BASE_URL, url_path)
    
    resp = fetch_with_retry(scraper, full_url)
    
    if not resp:
        logger.error(f"  ✗ Не удалось загрузить: {full_url}")
        return None
    
    # Пробуем разные парсеры
    soup = None
    for parser in ['html.parser', 'lxml', 'html5lib']:
        try:
            soup = BeautifulSoup(resp.content, parser)
            # Проверяем что страница распарсилась
            if soup.find('title') or soup.find('h1'):
                break
        except:
            continue
    
    if not soup:
        logger.error(f"  ✗ Не удалось распарсить HTML: {full_url}")
        return None
    
    film_data = {}
    
    # Код фильма из URL
    path_parts = url_path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[-2] == 'movies':
        film_data['code'] = path_parts[-1].upper()
    else:
        logger.error(f"  ✗ Не удалось извлечь код из URL: {url_path}")
        return None

    # Название - ищем разными способами
    title = None
    
    # Способ 1: h1 тег
    h1_tag = soup.find('h1')
    if h1_tag:
        title = h1_tag.get_text(strip=True)
        logger.debug(f"  Найден h1: {title[:80]}")
    
    # Способ 2: meta og:title
    if not title:
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].strip()
            logger.debug(f"  Найден og:title: {title[:80]}")
    
    # Способ 3: title тег
    if not title:
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
            # Убираем " - JAV Database" из конца
            title = title.replace(' - JAV Database', '').strip()
            logger.debug(f"  Найден title: {title[:80]}")
    
    film_data['title'] = title if title else 'No Title'

    # Описание
    description = ''
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        description = meta_desc['content'].strip()
    else:
        og_desc = soup.find('meta', property='og:description')
        if og_desc and og_desc.get('content'):
            description = og_desc['content'].strip()
    film_data['description'] = description[:500] if description else ''

    # Обложка
    thumbnail = None
    
    # Способ 1: og:image
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        thumbnail = urlparse(og_image['content']).path
    
    # Способ 2: poster-container
    if not thumbnail:
        poster = soup.find('div', id='poster-container')
        if poster:
            img = poster.find('img')
            if img and img.get('src'):
                thumbnail = urlparse(urljoin(BASE_URL, img['src'])).path
    
    # Способ 3: любая большая картинка
    if not thumbnail:
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if '/covers/' in src or '/digital/' in src:
                thumbnail = urlparse(urljoin(BASE_URL, src)).path
                break
    
    film_data['thumbnail'] = thumbnail

    # Скриншоты
    screenshots = []
    
    # Способ 1: image-gallery-section
    gallery = soup.find('div', class_='image-gallery-section')
    if gallery:
        for a_tag in gallery.find_all('a', attrs={'data-image-src': True}):
            screenshots.append(urlparse(a_tag['data-image-src']).path)
    
    # Способ 2: все ссылки на pics.dmm.co.jp
    if not screenshots:
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if 'pics.dmm.co.jp' in href and href.endswith('.jpg'):
                screenshots.append(urlparse(href).path)
    
    film_data['screenshots'] = screenshots[:10]

    # Метаданные
    genres = []
    actresses = []
    
    movie_table = soup.find('div', class_='movietable')
    if movie_table:
        for row in movie_table.find_all(['p', 'div']):
            text = row.get_text(strip=True)
            
            if 'Genre(s):' in text or 'Genre:' in text:
                for a in row.find_all('a', rel='tag'):
                    genres.append(a.get_text(strip=True))
            
            if 'Idol(s)/Actress(es):' in text or 'Actress(es):' in text:
                for a in row.find_all('a'):
                    actresses.append(a.get_text(strip=True))
    
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
    logger.info("Запуск парсера JAVDatabase")
    if TEST_MODE:
        logger.info(f"ТЕСТОВЫЙ РЕЖИМ: парсим только {TEST_LIMIT} фильмов")
    logger.info("="*60)
    
    # Проверяем ключ
    test_data = {"test": "ok"}
    try:
        temp_file = "test_enc.tmp"
        save_encrypted_json(test_data, temp_file, XOR_KEY)
        loaded = load_encrypted_json(temp_file, XOR_KEY)
        os.remove(temp_file)
        if loaded != test_data:
            logger.error("Ошибка ключа шифрования!")
            return
    except Exception as e:
        logger.error(f"Ошибка шифрования: {e}")
        return
    
    # Загружаем существующие данные
    existing_data = load_encrypted_json(ENCRYPTED_FILE, XOR_KEY)
    existing_films = existing_data.get('films', []) if existing_data else []
    existing_codes = {film['code'] for film in existing_films}
    logger.info(f"Существующих фильмов в базе: {len(existing_films)}")
    
    # Получаем URL фильмов
    film_paths = get_sitemap_urls()
    
    if not film_paths:
        logger.warning("Нет URL для парсинга")
        return
    
    # В тестовом режиме берем только первые N
    if TEST_MODE:
        film_paths = film_paths[:TEST_LIMIT]
        logger.info(f"Тестовый режим: {len(film_paths)} фильмов")
    
    # Фильтруем только новые
    new_paths = []
    for path in film_paths:
        path_parts = path.strip('/').split('/')
        if len(path_parts) >= 2:
            code = path_parts[-1].upper()
            if code not in existing_codes:
                new_paths.append(path)
    
    logger.info(f"Новых фильмов для парсинга: {len(new_paths)} из {len(film_paths)}")
    
    if not new_paths:
        logger.info("Нет новых фильмов. Завершение.")
        return
    
    # Создаем scraper
    scraper = create_scraper()
    
    # Парсим
    new_films = []
    total = len(new_paths)
    
    for i, path in enumerate(new_paths, 1):
        logger.info(f"[{i}/{total}] {path}")
        
        film_data = parse_film_page(scraper, path)
        
        if film_data:
            new_films.append(film_data)
            logger.info(f"  ✓ {film_data['code']}: {film_data['title'][:60]}")
        else:
            logger.warning(f"  ✗ Не удалось спарсить")
        
        # Задержка
        time.sleep(random.uniform(1.5, 3))
        
        # Промежуточное сохранение
        if len(new_films) % 50 == 0 and new_films:
            all_films = existing_films + new_films
            output_data = {
                "films": all_films,
                "metadata": {
                    "version": "1.0.0",
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "source": "javdatabase.com",
                    "totalFilms": len(all_films),
                    "newFilms": len(new_films)
                }
            }
            save_encrypted_json(output_data, ENCRYPTED_FILE, XOR_KEY)
            logger.info(f"  💾 Сохранено: {len(all_films)} фильмов")
    
    # Финальное сохранение
    all_films = existing_films + new_films
    output_data = {
        "films": all_films,
        "metadata": {
            "version": "1.0.0",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "javdatabase.com",
            "totalFilms": len(all_films),
            "newFilms": len(new_films)
        }
    }
    
    save_encrypted_json(output_data, ENCRYPTED_FILE, XOR_KEY)
    
    # В тестовом режиме сохраняем и читаемый JSON
    if TEST_MODE:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        logger.info(f"Тестовый JSON сохранен в {OUTPUT_FILE}")
    
    elapsed = time.time() - start_time
    logger.info("="*60)
    logger.info(f"Готово! Новых: {len(new_films)}, всего: {len(all_films)}")
    logger.info(f"Время: {elapsed/60:.1f} мин")
    logger.info("="*60)

if __name__ == "__main__":
    main()
