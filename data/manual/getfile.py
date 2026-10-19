#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Универсальный сборщик эпизодов с promodj.com.
Поддерживает несколько групп-источников (BASE_URLS) — обходятся по очереди.
Все параметры конкретного подкаста — в блоке КОНФИГ ниже.
"""

import requests
from bs4 import BeautifulSoup
import time
import re
import sys
import json
import os
import html
import logging
from datetime import datetime
from urllib.parse import urlparse, urlunparse

# =====================================================================
# ============================== КОНФИГ ===============================
# =====================================================================
# Меняйте только этот блок при переходе на другой подкаст.

# --- Кто автор подкаста (одно место для всего) ---
ARTIST_SLUG = "jim"     # используется в URL-ах (нижний регистр)
ARTIST_NAME = "jim"     # отображаемое имя (в <itunes:author> и имени файла)

# --- URL-ы источников (несколько групп-пейджингов) ---
# Скрипт обойдёт их по очереди, соберёт все найденные эпизоды и удалит дубли.
BASE_URLS = [
    "https://promodj.com/jim/groups/606077/ElectroMehanika",
    # добавьте сюда другие группы этого же автора:
    # f"https://promodj.com/{ARTIST_SLUG}/groups/12345/Other_Group_Name",
]

PODCAST_URL = f"https://promodj.com/{ARTIST_SLUG}/podcast.xml"

# --- Имена файлов (по умолчанию — от ARTIST_NAME) ---
OUTPUT_FILE    = f"{ARTIST_NAME}.xml"
LOCAL_TEMPLATE = "podcast_template.xml"
PROGRESS_FILE  = "episodes_progress.json"
LOG_FILE       = "parser.log"

# --- Автор для <itunes:author> ---
PODCAST_AUTHOR = ARTIST_NAME

# --- Как фильтровать ссылки на эпизоды на странице группы ---
# Ссылка считается эпизодом, если в её href одновременно есть эти подстроки.
EPISODE_LINK_MARKERS = ["/radioshows/", f"/{ARTIST_SLUG}/"]
# EPISODE_LINK_MARKERS = ["/podcasts/", f"/{ARTIST_SLUG}/"]
# EPISODE_LINK_MARKERS = ["/tracks/", f"/{ARTIST_SLUG}/"]

# --- Как вытащить номер эпизода из URL (для сортировки от новых к старым) ---
EPISODE_NUMBER_REGEX = r"_(\d+)(?:[?#]|$)"

# --- Задержки / ограничения ---
DELAY      = 0.05
SAVE_EVERY = 25
MAX_PAGES  = 200

# --- User-Agent ---
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
# =====================================================================
# ============================ /КОНФИГ ================================
# =====================================================================

# ============================ ЛОГИРОВАНИЕ ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class ProgressTracker:
    """Считает проценты, ETA и среднюю скорость."""

    def __init__(self, total, name="Задача"):
        self.total = total
        self.name = name
        self.start = time.time()
        self.done = 0

    def tick(self, extra=""):
        self.done += 1
        elapsed = time.time() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.done) / rate if rate > 0 else 0
        percent = self.done / self.total * 100 if self.total else 0
        eta_str = time.strftime("%H:%M:%S", time.gmtime(remaining))
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        logger.info(
            f"[{self.name}] {self.done}/{self.total} ({percent:5.1f}%) | "
            f"прошло: {elapsed_str} | осталось: ~{eta_str} | {rate:.2f} шт/с"
            + (f" | {extra}" if extra else "")
        )

    def finish(self):
        elapsed = time.time() - self.start
        logger.info(
            f"[{self.name}] ЗАВЕРШЕНО: {self.done}/{self.total} "
            f"за {time.strftime('%H:%M:%S', time.gmtime(elapsed))}"
        )


# ============================ ХЕЛПЕРЫ ==============================
def fetch(url, retries=3, backoff=5):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            logger.warning(f"HTTP {r.status_code} для {url} (попытка {attempt}/{retries})")
        except requests.RequestException as e:
            logger.warning(f"Сетевая ошибка для {url}: {e} (попытка {attempt}/{retries})")
        if attempt < retries:
            time.sleep(backoff * attempt)
    return None


def episode_sort_key(url):
    m = re.search(EPISODE_NUMBER_REGEX, url)
    return int(m.group(1)) if m else 0


def xml_escape(s):
    return html.escape(s or "", quote=False)


def rfc822_date(raw):
    if raw:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[: len(fmt) + 6], fmt)
                return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except (ValueError, TypeError):
                continue
        months = {
            "января": "Jan", "февраля": "Feb", "марта": "Mar", "апреля": "Apr",
            "мая": "May", "июня": "Jun", "июля": "Jul", "августа": "Aug",
            "сентября": "Sep", "октября": "Oct", "ноября": "Nov", "декабря": "Dec",
        }
        m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", raw)
        if m and m.group(2) in months:
            try:
                dt = datetime.strptime(
                    f"{m.group(1)} {months[m.group(2)]} {m.group(3)}", "%d %b %Y"
                )
                return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except ValueError:
                pass
    return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")


def clean_episode_url(href):
    parsed = urlparse(href)
    return urlunparse(parsed._replace(query="", fragment=""))


def is_episode_link(href):
    return all(marker in href for marker in EPISODE_LINK_MARKERS)


# ======================= ЭТАП 1: ШАБЛОН ============================
def download_template():
    logger.info("Скачиваю существующий podcast.xml ...")
    r = fetch(PODCAST_URL)
    if r is None:
        logger.error("Не удалось скачать podcast.xml")
        sys.exit(1)
    with open(LOCAL_TEMPLATE, "w", encoding="utf-8") as f:
        f.write(r.text)
    logger.info(f"Шаблон сохранён: {LOCAL_TEMPLATE} ({len(r.text)} байт)")
    return r.text


def extract_existing_links(xml_text):
    links = set()
    for m in re.finditer(r"<link>\s*(.*?)\s*</link>", xml_text):
        url = m.group(1).strip()
        if "/radioshows/" in url or "/podcasts/" in url:
            links.add(url)
    logger.info(f"В шаблоне уже есть {len(links)} эпизодов")
    return links


# ============ ЭТАП 2: ОБХОД ВСЕХ ГРУПП-ИСТОЧНИКОВ =================
def collect_links_from_one_base(base_url):
    """Обходит пейджинг одной группы и возвращает set найденных ссылок."""
    episode_links = set()
    page = 1
    empty_pages = 0

    logger.info(f"  → Обход группы: {base_url}")

    while page <= MAX_PAGES:
        url = f"{base_url}?page={page}"
        r = fetch(url)
        if r is None:
            logger.warning(f"  Страница {page} не загрузилась, пропускаю")
            empty_pages += 1
            if empty_pages >= 3:
                break
            page += 1
            time.sleep(DELAY)
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if is_episode_link(href):
                clean = clean_episode_url(href)
                if clean not in episode_links:
                    episode_links.add(clean)
                    found += 1

        logger.info(
            f"     стр.{page}: +{found} новых (в этой группе: {len(episode_links)})"
        )

        if found == 0:
            empty_pages += 1
            if empty_pages >= 2:
                logger.info(f"     Пустые страницы — завершаю обход {base_url}")
                break
        else:
            empty_pages = 0

        page += 1
        time.sleep(DELAY)

    return episode_links


def collect_episode_links():
    """Обходит все BASE_URLS по очереди, объединяет и дедуплицирует."""
    all_links = set()

    for idx, base_url in enumerate(BASE_URLS, 1):
        logger.info(f"Этап 2/4 — источник {idx}/{len(BASE_URLS)}")
        try:
            links = collect_links_from_one_base(base_url)
        except Exception as e:
            logger.exception(f"Ошибка при обходе {base_url}: {e}")
            continue

        before = len(all_links)
        all_links |= links
        added = len(all_links) - before
        logger.info(
            f"  ← из группы получено {len(links)} ссылок "
            f"(из них новых для общего набора: {added})"
        )

    return sorted(all_links, key=episode_sort_key, reverse=True)


# =================== ЭТАП 3: ПАРСИНГ ЭПИЗОДА ======================
def parse_episode(url):
    r = fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag else ""

    desc = ""
    meta_desc = soup.find("meta", property="og:description")
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc["content"].strip()
    else:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            desc = meta_desc["content"].strip()

    pub_date = None
    meta_date = soup.find("meta", property="article:published_time")
    if meta_date and meta_date.get("content"):
        pub_date = meta_date["content"]
    else:
        m = re.search(
            r"(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
            r"сентября|октября|ноября|декабря)\s+\d{4})",
            soup.get_text(),
        )
        if m:
            pub_date = m.group(1)

    audio_url = ""
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".mp3"):
            audio_url = a["href"]
            break
    if not audio_url:
        meta_audio = soup.find("meta", property="og:audio")
        if meta_audio and meta_audio.get("content"):
            audio_url = meta_audio["content"]

    return {
        "title": title,
        "link": url,
        "guid": url,
        "pubDate": pub_date or "",
        "description": desc,
        "audio_url": audio_url,
    }


# =================== ЭТАП 4: ГЕНЕРАЦИЯ XML ========================
def build_item_xml(ep):
    title = xml_escape(ep["title"])
    link = xml_escape(ep["link"])
    guid = xml_escape(ep["guid"])
    desc = xml_escape(ep["description"])
    pub = rfc822_date(ep["pubDate"])
    audio = ep.get("audio_url") or ""
    author = xml_escape(PODCAST_AUTHOR)

    parts = [
        "    <item>",
        f"      <title>{title}</title>",
        f"      <link>{link}</link>",
        f"      <guid>{guid}</guid>",
        f"      <pubDate>{pub}</pubDate>",
        f"      <description>{desc}</description>",
        f"      <itunes:author>{author}</itunes:author>",
        f"      <itunes:subtitle>{title}</itunes:subtitle>",
        f"      <itunes:summary>{desc}</itunes:summary>",
        f"      <itunes:explicit>no</itunes:explicit>",
    ]
    if audio:
        parts.append(
            f'      <enclosure url="{xml_escape(audio)}" type="audio/mpeg" length="0"/>'
        )
    parts.append("    </item>")
    return "\n".join(parts)


def build_final_xml(template_xml, new_episodes):
    if not new_episodes:
        logger.info("Нет новых эпизодов для добавления")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(template_xml)
        return

    items_xml = "\n".join(build_item_xml(ep) for ep in new_episodes)

    if "</channel>" not in template_xml:
        logger.error("В шаблоне нет </channel> — неожиданная структура")
        sys.exit(1)

    result = template_xml.replace("</channel>", items_xml + "\n  </channel>", 1)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(result)
    logger.info(f"Записано: {OUTPUT_FILE}")


# ============================= MAIN ===============================
def main():
    logger.info("=" * 60)
    logger.info(f"СТАРТ ПАРСИНГА: {OUTPUT_FILE}")
    logger.info(f"Источников в BASE_URLS: {len(BASE_URLS)}")
    for i, u in enumerate(BASE_URLS, 1):
        logger.info(f"  [{i}] {u}")
    logger.info(f"RSS: {PODCAST_URL}")
    logger.info("=" * 60)

    logger.info("Этап 1/4: загрузка шаблона podcast.xml")
    template_xml = download_template()
    existing_links = extract_existing_links(template_xml)

    logger.info("Этап 2/4: сбор ссылок со страниц пейджинга всех источников")
    all_links = collect_episode_links()
    logger.info(f"Всего уникальных ссылок найдено: {len(all_links)}")

    missing_links = [l for l in all_links if l not in existing_links]
    logger.info(f"Отсутствует в шаблоне: {len(missing_links)}")

    if not missing_links:
        logger.info("Всё уже есть в шаблоне — нечего добавлять")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(template_xml)
        return

    logger.info("Этап 3/4: парсинг отсутствующих эпизодов")

    episodes_data = []
    done_links = set()
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                episodes_data = json.load(f)
            done_links = {ep["link"] for ep in episodes_data}
            logger.info(f"Загружен промежуточный прогресс: {len(episodes_data)} шт.")
        except Exception as e:
            logger.warning(f"Не удалось загрузить прогресс: {e}")

    tracker = ProgressTracker(len(missing_links), name="Парсинг")

    for link in missing_links:
        if link in done_links:
            tracker.tick(extra="↷ уже собран")
            continue
        try:
            data = parse_episode(link)
            if data is None:
                logger.warning(f"Не удалось получить данные: {link}")
                tracker.tick(extra="✗ ошибка")
                continue
            episodes_data.append(data)
            tracker.tick(extra=f"✓ {data['title'][:60]}")

            if len(episodes_data) % SAVE_EVERY == 0:
                with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                    json.dump(episodes_data, f, ensure_ascii=False, indent=2)
                logger.info(f"  → Промежуточное сохранение ({len(episodes_data)})")
        except Exception as e:
            logger.exception(f"Ошибка на {link}: {e}")
            tracker.tick(extra="✗ исключение")

        time.sleep(DELAY)

    tracker.finish()

    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(episodes_data, f, ensure_ascii=False, indent=2)

    logger.info("Этап 4/4: сборка итогового XML")
    episodes_data.sort(key=lambda ep: episode_sort_key(ep["link"]), reverse=True)
    build_final_xml(template_xml, episodes_data)

    logger.info("=" * 60)
    logger.info(f"ГОТОВО. Добавлено эпизодов: {len(episodes_data)}")
    logger.info(f"Итоговый файл: {OUTPUT_FILE}")
    logger.info(f"Лог: {LOG_FILE}")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Прервано пользователем (Ctrl+C)")
        sys.exit(1)
