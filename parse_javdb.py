import cloudscraper
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import json
import time
import os
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

# Разные User-Agent для ротации
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
]

REQUEST_TIMEOUT = 60  # увеличенный таймаут для cloudscraper
DELAY_BETWEEN_REQUESTS = 2  # увеличенная задержка
MAX_RETRIES = 3

def create_scraper():
    """Создает cloudscraper с случайным User-Agent"""
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        },
        delay=10  # ждем пока cloudscraper решит challenge
    )
    
    # Устанавливаем случайный User-Agent
    scraper.headers.update({
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })
    
    return scraper

def fetch_with_retry(scraper, url, max_retries=MAX_RETRIES):
    """Загружает URL с повторными попытками и разными User-Agent"""
    for attempt in range(max_retries):
        try:
            logger.info(f"Попытка {attempt + 1}/{max_retries} для {url}")
            
            # Меняем User-Agent при повторных попытках
            if attempt > 0:
                scraper.headers.update({'User-Agent': random.choice(USER_AGENTS)})
                time.sleep(random.uniform(3, 7))  # случайная задержка
            
            response = scraper.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 403:
                logger.warning(f"403 Forbidden на попытке {attempt + 1}")
                if attempt < max_retries - 1:
                    # Ждем подольше перед следующей попыткой
                    wait_time = (attempt + 1) * 10
                    logger.info(f"Ожидание {wait_time} секунд...")
                    time.sleep(wait_time)
                continue
            else:
                logger.warning(f"Статус {response.status_code} на попытке {attempt + 1}")
                
        except Exception as e:
            logger.error(f"Ошибка на попытке {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(random.uniform(5, 10))
            continue
    
    logger.error(f"Не удалось загрузить {url} после {max_retries} попыток")
    return None

def get_sitemap_urls():
    """
    Этап 1: Парсинг Sitemap с использованием cloudscraper
    """
    scraper = create_scraper()
    
    logger.info(f"Загрузка главного sitemap-индекса: {SITEMAP_INDEX_URL}")
    
    # Пробуем загрузить sitemap index
    resp = fetch_with_retry(scraper, SITEMAP_INDEX_URL)
    
    if not resp:
        logger.error("Не удалось получить sitemap index")
        return get_cached_film_paths()
    
    logger.info(f"Sitemap index загружен успешно. Размер: {len(resp.content)} байт")
    
    # Парсим XML
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга XML: {e}")
        return get_cached_film_paths()
    
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    sitemaps = []
    
    for sitemap in root.findall('sm:sitemap', ns):
        loc_elem = sitemap.find('sm:loc', ns)
        lastmod_elem = sitemap.find('sm:lastmod', ns)
        
        if loc_elem is not None:
            loc = loc_elem.text
            lastmod = lastmod_elem.text if lastmod_elem is not None else None
            sitemaps.append({'loc': loc, 'lastmod': lastmod})

    logger.info(f"Найдено {len(sitemaps)} файлов sitemap в индексе:")
    for s in sitemaps:
        logger.info(f"  - {s['loc']} (lastmod: {s['lastmod']})")

    # Загрузка кэша
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            logger.info(f"Кэш загружен. Записей: {len(cache)}")
        except Exception as e:
            logger.warning(f"Ошибка загрузки кэша: {e}")

    all_film_paths = set()
    new_cache = {}

    for sitemap_data in sitemaps:
        loc = sitemap_data['loc']
        lastmod = sitemap_data['lastmod']
        new_cache[loc] = lastmod

        # Обрабатываем только файлы с фильмами
        if not ('movies-sitemap' in loc or 'post-sitemap' in loc):
            logger.info(f"Пропуск (не фильмы): {loc}")
            continue
        
        # Проверяем кэш
        if loc in cache and cache[loc] == lastmod and lastmod is not None:
            logger.info(f"Пропуск (без изменений): {loc}")
            continue
        
        logger.info(f"Обработка: {loc}")
        
        # Загружаем дочерний sitemap
        time.sleep(random.uniform(3, 5))  # пауза между запросами sitemap
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
                    film_loc = film_loc_elem.text
                    parsed_url = urlparse(film_loc)
                    # Сохраняем только пути к фильмам
                    if '/movies/' in parsed_url.path:
                        all_film_paths.add(parsed_url.path)
                        urls_found += 1
            
            logger.info(f"  -> Найдено URL фильмов: {urls_found} (всего уникальных: {len(all_film_paths)})")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке {loc}: {e}")

    # Сохраняем кэш
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_cache, f, indent=2)
        logger.info(f"Кэш обновлен.")
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша: {e}")
    
    logger.info(f"Всего URL фильмов для парсинга: {len(all_film_paths)}")
    return list(all_film_paths)

def get_cached_film_paths():
    """Извлекает пути фильмов из существующего output файла или кэша"""
    paths = []
    
    # Пробуем из output файла
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                paths = [f"/movies/{film['code'].lower()}/" for film in data.get('films', [])]
                logger.info(f"Восстановлено {len(paths)} путей из {OUTPUT_FILE}")
                return paths
        except Exception as e:
            logger.error(f"Ошибка чтения {OUTPUT_FILE}: {e}")
    
    # Пробуем из кэша sitemap
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                logger.info(f"Найдены sitemap в кэше: {list(cache.keys())}")
        except Exception as e:
            logger.error(f"Ошибка чтения кэша: {e}")
    
    return paths

def parse_film_page(scraper, url_path):
    """
    Этап 2: Парсинг страницы фильма
    """
    full_url = urljoin(BASE_URL, url_path)
    
    resp = fetch_with_retry(scraper, full_url, max_retries=2)
    
    if not resp:
        return None
    
    soup = BeautifulSoup(resp.content, 'html.parser')
    film_data = {}
    
    # --- Код фильма из URL ---
    path_parts = url_path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[-2] == 'movies':
        film_data['code'] = path_parts[-1].upper()
    else:
        return None

    # --- Название ---
    title_tag = soup.find('h1')
    if title_tag:
        film_data['title'] = title_tag.text.strip()
    else:
        og_title = soup.find('meta', property='og:title')
        film_data['title'] = og_title['content'].strip() if og_title else 'No Title'

    # --- Описание ---
    description_meta = soup.find('meta', attrs={'name': 'description'})
    if description_meta and description_meta.get('content'):
        film_data['description'] = description_meta['content'].strip()
    else:
        film_data['description'] = ''

    # --- Обложка ---
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        film_data['thumbnail'] = urlparse(og_image['content']).path
    else:
        # Ищем в poster-container
        poster = soup.find('div', id='poster-container')
        if poster:
            img = poster.find('img')
            if img and img.get('src'):
                film_data['thumbnail'] = urlparse(urljoin(BASE_URL, img['src'])).path
            else:
                film_data['thumbnail'] = None
        else:
            film_data['thumbnail'] = None

    # --- Скриншоты ---
    screenshots = []
    gallery = soup.find('div', class_='image-gallery-section')
    if gallery:
        for a_tag in gallery.find_all('a', attrs={'data-image-src': True}):
            img_url = a_tag['data-image-src']
            screenshots.append(urlparse(img_url).path)
    film_data['screenshots'] = screenshots

    # --- Метаданные ---
    genres = []
    actresses = []
    
    movie_table = soup.find('div', class_='movietable')
    if movie_table:
        for row in movie_table.find_all('p', class_='mb-1'):
            text = row.get_text(strip=True)
            if text.startswith('Genre(s):'):
                genre_links = row.find_all('a', rel='tag')
                genres = [a.text.strip() for a in genre_links]
            elif text.startswith('Idol(s)/Actress(es):'):
                actress_links = row.find_all('a')
                if actress_links:
                    actresses = [a.text.strip() for a in actress_links]
                else:
                    parts = text.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        actresses = [name.strip() for name in parts[1].split(',')]

    film_data['metadata'] = {
        'genre': genres,
        'actress': actresses
    }

    # --- Дата ---
    date_added = None
    if movie_table:
        for row in movie_table.find_all('p', class_='mb-1'):
            text = row.get_text(strip=True)
            if text.startswith('Release Date:'):
                date_str = text.replace('Release Date:', '').strip()
                try:
                    dt = datetime.strptime(date_str, '%Y-%m-%d')
                    date_added = dt.replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
    
    film_data['dateAdded'] = date_added or datetime.now(timezone.utc).isoformat()

    return film_data

def main():
    start_time = time.time()
    logger.info("="*60)
    logger.info("Запуск парсера JAVDatabase")
    logger.info("="*60)
    
    # Этап 1: Получаем список путей
    film_paths = get_sitemap_urls()
    
    if not film_paths:
        logger.warning("Не удалось получить пути фильмов.")
        logger.info("Проверьте, существует ли файл parsed_films.json с предыдущими данными")
        return
    
    logger.info(f"Получено {len(film_paths)} путей фильмов для обработки")
    
    # Создаем новый scraper для парсинга страниц
    scraper = create_scraper()
    
    # Этап 2: Парсим страницы
    parsed_films = []
    total = len(film_paths)
    
    for i, path in enumerate(film_paths, 1):
        if i % 10 == 0:
            logger.info(f"Прогресс: {i}/{total} ({i/total*100:.1f}%)")
        
        film_data = parse_film_page(scraper, path)
        if film_data:
            parsed_films.append(film_data)
            logger.info(f"  ✓ {film_data['code']}: {film_data['title'][:50]}...")
        else:
            logger.warning(f"  ✗ Не удалось спарсить: {path}")
        
        # Случайная задержка
        time.sleep(random.uniform(1.5, 3))
        
        # Сохраняем промежуточные результаты каждые 50 фильмов
        if i % 50 == 0:
            output_data = {
                "films": parsed_films,
                "metadata": {
                    "version": "1.0.0",
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "source": "javdatabase.com"
                }
            }
            try:
                with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                    json.dump(output_data, f, ensure_ascii=False, indent=2)
                logger.info(f"Промежуточное сохранение: {len(parsed_films)} фильмов")
            except Exception as e:
                logger.error(f"Ошибка сохранения: {e}")
    
    # Финальное сохранение
    output_data = {
        "films": parsed_films,
        "metadata": {
            "version": "1.0.0",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "javdatabase.com"
        }
    }
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    elapsed_time = time.time() - start_time
    logger.info("="*60)
    logger.info(f"Парсинг завершен!")
    logger.info(f"Обработано фильмов: {len(parsed_films)}/{total}")
    logger.info(f"Время выполнения: {elapsed_time/60:.1f} минут")
    logger.info(f"Результат сохранен в: {OUTPUT_FILE}")
    logger.info("="*60)

if __name__ == "__main__":
    main()
