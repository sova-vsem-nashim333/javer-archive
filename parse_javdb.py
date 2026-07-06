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

# --- Настройка логирования для GitHub Actions ---
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

# Актуальный User-Agent, чтобы нас не банили
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; JavDatabaseParser/1.0; +https://github.com/your-repo)'
}
# Таймауты для "долго грузящегося" сайта
REQUEST_TIMEOUT = (60, 120)  # (connection timeout, read timeout)
# Пауза между запросами страниц фильмов (чтобы не нагружать сервер)
DELAY_BETWEEN_REQUESTS = 1.0

def get_sitemap_urls():
    """
    Этап 1: Парсинг Sitemap.
    Проверяет кэш и загружает только измененные sitemap-файлы.
    Возвращает полный список URL-путей к фильмам (без домена).
    """
    logger.info(f"Загрузка главного sitemap-индекса: {SITEMAP_INDEX_URL}")
    resp = requests.get(SITEMAP_INDEX_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    # Определяем namespace, если он есть
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    sitemaps = []
    for sitemap in root.findall('sm:sitemap', ns):
        loc = sitemap.find('sm:loc', ns).text
        lastmod = sitemap.find('sm:lastmod', ns).text
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

        # Пропускаем, если в кэше та же дата и файл не 'post-sitemap.xml' или 'movies-sitemap'
        if 'movies-sitemap' in loc or 'post-sitemap' in loc:
            if loc in cache and cache[loc] == lastmod:
                logger.info(f"Пропуск (без изменений): {loc}")
                continue
            else:
                logger.info(f"Обработка (изменен или новый): {loc}")
        else:
            logger.info(f"Пропуск (не относится к фильмам): {loc}")
            continue

        # Загружаем и парсим дочерний sitemap
        try:
            resp = requests.get(loc, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            sitemap_root = ET.fromstring(resp.content)
            
            for url in sitemap_root.findall('sm:url', ns):
                film_loc = url.find('sm:loc', ns).text
                parsed_url = urlparse(film_loc)
                # Сохраняем путь БЕЗ ДОМЕНА
                all_film_paths.add(parsed_url.path)

            logger.info(f"  -> Извлечено URL из {loc}: {len(all_film_paths)} (накопительно)")
        except Exception as e:
            logger.error(f"Ошибка при обработке {loc}: {e}")

    # Сохраняем новый кэш
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_cache, f, indent=2)
    logger.info(f"Кэш обновлен. Всего URL для парсинга: {len(all_film_paths)}")

    return list(all_film_paths)

def parse_film_page(url_path):
    """
    Этап 2: Парсинг страницы фильма.
    Извлекает данные из HTML и возвращает словарь.
    """
    full_url = urljoin(BASE_URL, url_path)
    logger.info(f"Парсинг страницы: {full_url}")
    
    try:
        resp = requests.get(full_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
    except Exception as e:
        logger.error(f"Ошибка загрузки {full_url}: {e}")
        return None

    film_data = {}
    
    # --- Извлечение кода фильма из URL ---
    path_parts = url_path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[-2] == 'movies':
        film_data['code'] = path_parts[-1].upper()
    else:
        logger.warning(f"Не удалось извлечь код из URL: {url_path}")
        return None

    # --- 1. Название ---
    title_tag = soup.find('h1')
    if title_tag:
        film_data['title'] = title_tag.text.strip()
    else:
        # Fallback на meta og:title
        og_title = soup.find('meta', property='og:title')
        film_data['title'] = og_title['content'].strip() if og_title else 'No Title'

    # --- 2. Описание ---
    description_meta = soup.find('meta', attrs={'name': 'description'})
    if description_meta and description_meta.get('content'):
        film_data['description'] = description_meta['content'].strip()
    else:
        # Fallback на og:description
        og_desc = soup.find('meta', property='og:description')
        film_data['description'] = og_desc['content'].strip() if og_desc else ''

    # --- 3. Обложка (Thumbnail) ---
    thumbnail_img = soup.find('div', id='poster-container')
    if thumbnail_img:
        img_tag = thumbnail_img.find('img')
        if img_tag and img_tag.get('src'):
            thumbnail_url = urljoin(BASE_URL, img_tag['src'])
            film_data['thumbnail'] = urlparse(thumbnail_url).path
        else:
            film_data['thumbnail'] = None
    else:
        film_data['thumbnail'] = None

    # --- 4. Скриншоты ---
    screenshots = []
    gallery_div = soup.find('div', class_='image-gallery-section')
    if gallery_div:
        for a_tag in gallery_div.find_all('a', attrs={'data-image-src': True}):
            img_url = a_tag['data-image-src']
            # Сохраняем путь без домена
            screenshots.append(urlparse(img_url).path)
    film_data['screenshots'] = screenshots

    # --- 5. Метаданные (жанры и актрисы) ---
    genres = []
    actresses = []
    
    # Ищем строки в таблице movietable
    movie_table = soup.find('div', class_='movietable')
    if movie_table:
        rows = movie_table.find_all('p', class_='mb-1')
        for row in rows:
            text = row.get_text(strip=True)
            if text.startswith('Genre(s):'):
                # Жанры
                genre_links = row.find_all('a', rel='tag')
                genres = [a.text.strip() for a in genre_links]
            elif text.startswith('Idol(s)/Actress(es):'):
                # Актрисы (могут быть ссылками или просто текстом)
                actress_links = row.find_all('a')
                if actress_links:
                    actresses = [a.text.strip() for a in actress_links]
                else:
                    # Если нет ссылок, пытаемся извлечь чистый текст после ':'
                    parts = text.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        actresses = [name.strip() for name in parts[1].split(',')]

    film_data['metadata'] = {
        'genre': genres,
        'actress': actresses
    }

    # --- 6. Дата добавления ---
    # Пытаемся найти дату релиза на странице
    date_added = None
    if movie_table:
        for row in movie_table.find_all('p', class_='mb-1'):
            text = row.get_text(strip=True)
            if text.startswith('Release Date:'):
                date_str = text.replace('Release Date:', '').strip()
                try:
                    # Парсим как "2026-08-05" и приводим к нужному формату
                    dt = datetime.strptime(date_str, '%Y-%m-%d')
                    date_added = dt.replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
    # Если дата не найдена, используем текущую
    film_data['dateAdded'] = date_added or datetime.now(timezone.utc).isoformat()

    return film_data

def main():
    start_time = time.time()
    logger.info("Запуск парсера JAVDatabase.")
    
    # Этап 1: Получаем список путей
    film_paths = get_sitemap_urls()
    
    # Если хотите протестировать на небольшом количестве, раскомментируйте:
    # film_paths = film_paths[:10]
    
    # Этап 2: Парсим страницы
    parsed_films = []
    total = len(film_paths)
    
    for i, path in enumerate(film_paths, 1):
        logger.info(f"Обработка {i}/{total}: {path}")
        film_data = parse_film_page(path)
        if film_data:
            parsed_films.append(film_data)
        
        # Пауза между запросами
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # Формируем итоговую структуру
    output_data = {
        "films": parsed_films,
        "metadata": {
            "version": "1.0.0",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "javdatabase.com"
        }
    }
    
    # Сохраняем в JSON
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    elapsed_time = time.time() - start_time
    logger.info(f"Парсинг завершен. Обработано фильмов: {len(parsed_films)}. Время выполнения: {elapsed_time:.2f} сек.")
    logger.info(f"Результат сохранен в {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
