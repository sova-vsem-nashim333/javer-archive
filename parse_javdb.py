import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import json
import time
import os
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
import logging
import sys
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

# Более реалистичные заголовки, имитирующие обычный браузер
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Sec-Ch-Ua': '"Not/A)Brand";v="99", "Google Chrome";v="126", "Chromium";v="126"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}

# Таймауты
REQUEST_TIMEOUT = (30, 60)  # (connect, read)
DELAY_BETWEEN_REQUESTS = 1.5  # Увеличим задержку

def create_session():
    """Создает requests сессию с retry механизмом"""
    session = requests.Session()
    
    # Настройка retry стратегии
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Устанавливаем заголовки по умолчанию
    session.headers.update(HEADERS)
    
    return session

def fetch_with_cookies(session, url):
    """
    Загружает URL с предварительным запросом главной страницы для получения cookies
    """
    try:
        # Сначала заходим на главную страницу, чтобы получить cookies (как обычный браузер)
        logger.info("Получение cookies с главной страницы...")
        main_page = session.get(
            BASE_URL,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )
        logger.info(f"Главная страница: статус {main_page.status_code}")
        
        # Небольшая пауза как у реального пользователя
        time.sleep(2)
        
        # Теперь запрашиваем целевой URL
        logger.info(f"Запрос целевого URL: {url}")
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )
        response.raise_for_status()
        return response
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе {url}: {e}")
        raise

def get_sitemap_urls():
    """
    Этап 1: Парсинг Sitemap.
    Проверяет кэш и загружает только измененные sitemap-файлы.
    Возвращает полный список URL-путей к фильмам (без домена).
    """
    session = create_session()
    
    logger.info(f"Загрузка главного sitemap-индекса: {SITEMAP_INDEX_URL}")
    
    try:
        # Пробуем с cookies
        resp = fetch_with_cookies(session, SITEMAP_INDEX_URL)
    except Exception as e:
        logger.warning(f"Первая попытка не удалась: {e}")
        logger.info("Пробуем альтернативный подход...")
        
        # Альтернативный подход: прямой запрос без предварительного посещения
        try:
            resp = session.get(SITEMAP_INDEX_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e2:
            logger.error(f"Все попытки получить sitemap не удались: {e2}")
            # Возвращаем пути из кэша если есть
            return get_cached_film_paths()
    
    # Парсим XML
    root = ET.fromstring(resp.content)
    
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    sitemaps = []
    for sitemap in root.findall('sm:sitemap', ns):
        loc = sitemap.find('sm:loc', ns).text
        lastmod = sitemap.find('sm:lastmod', ns).text if sitemap.find('sm:lastmod', ns) is not None else None
        sitemaps.append({'loc': loc, 'lastmod': lastmod})

    logger.info(f"Найдено {len(sitemaps)} файлов sitemap в индексе.")

    # Загрузка кэша
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        logger.info(f"Кэш загружен. Записей: {len(cache)}")

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
        
        # Загружаем и парсим дочерний sitemap
        try:
            time.sleep(1)  # Пауза между запросами sitemap
            resp = session.get(loc, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            sitemap_root = ET.fromstring(resp.content)
            
            for url in sitemap_root.findall('sm:url', ns):
                film_loc = url.find('sm:loc', ns).text
                parsed_url = urlparse(film_loc)
                all_film_paths.add(parsed_url.path)

            logger.info(f"  -> Найдено URL: {len(all_film_paths)} (всего)")
        except Exception as e:
            logger.error(f"Ошибка при обработке {loc}: {e}")

    # Сохраняем кэш
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_cache, f, indent=2)
    
    logger.info(f"Кэш обновлен. Всего URL для парсинга: {len(all_film_paths)}")
    return list(all_film_paths)

def get_cached_film_paths():
    """Извлекает пути фильмов из существующего output файла"""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                paths = [f"/movies/{film['code'].lower()}/" for film in data.get('films', [])]
                logger.info(f"Восстановлено {len(paths)} путей из существующего {OUTPUT_FILE}")
                return paths
        except Exception as e:
            logger.error(f"Ошибка чтения {OUTPUT_FILE}: {e}")
    return []

def parse_film_page(session, url_path):
    """
    Этап 2: Парсинг страницы фильма.
    """
    full_url = urljoin(BASE_URL, url_path)
    logger.info(f"Парсинг: {full_url}")
    
    try:
        resp = session.get(full_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
    except Exception as e:
        logger.error(f"Ошибка загрузки {full_url}: {e}")
        return None

    film_data = {}
    
    # --- Код фильма из URL ---
    path_parts = url_path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[-2] == 'movies':
        film_data['code'] = path_parts[-1].upper()
    else:
        logger.warning(f"Не удалось извлечь код из URL: {url_path}")
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
    thumbnail_img = soup.find('div', id='poster-container')
    if thumbnail_img:
        img_tag = thumbnail_img.find('img')
        if img_tag and img_tag.get('src'):
            thumbnail_url = urljoin(BASE_URL, img_tag['src'])
            film_data['thumbnail'] = urlparse(thumbnail_url).path
        else:
            film_data['thumbnail'] = None
    else:
        # Fallback на og:image
        og_image = soup.find('meta', property='og:image')
        if og_image:
            film_data['thumbnail'] = urlparse(og_image['content']).path
        else:
            film_data['thumbnail'] = None

    # --- Скриншоты ---
    screenshots = []
    gallery_div = soup.find('div', class_='image-gallery-section')
    if gallery_div:
        for a_tag in gallery_div.find_all('a', attrs={'data-image-src': True}):
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
    logger.info("Запуск парсера JAVDatabase.")
    
    # Создаем сессию
    session = create_session()
    
    # Этап 1: Получаем список путей
    film_paths = get_sitemap_urls()
    
    if not film_paths:
        logger.warning("Не удалось получить пути фильмов. Завершение.")
        return
    
    # Для теста можно ограничить количество
    # film_paths = film_paths[:10]
    
    # Этап 2: Парсим страницы
    parsed_films = []
    total = len(film_paths)
    
    for i, path in enumerate(film_paths, 1):
        logger.info(f"Обработка {i}/{total}: {path}")
        film_data = parse_film_page(session, path)
        if film_data:
            parsed_films.append(film_data)
        
        # Задержка между запросами
        time.sleep(DELAY_BETWEEN_REQUESTS)
        
        # Логируем прогресс каждые 100 фильмов
        if i % 100 == 0:
            logger.info(f"Прогресс: {i}/{total} ({i/total*100:.1f}%)")
    
    # Формируем итоговый JSON
    output_data = {
        "films": parsed_films,
        "metadata": {
            "version": "1.0.0",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "javdatabase.com"
        }
    }
    
    # Сохраняем
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    elapsed_time = time.time() - start_time
    logger.info(f"Парсинг завершен. Фильмов: {len(parsed_films)}. Время: {elapsed_time:.2f} сек.")
    logger.info(f"Результат сохранен в {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
