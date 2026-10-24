#!/usr/bin/env python3
"""
random-podcast.py — случайный эпизод из подкастов в OPML-файлах.

Возможности:
  * основной OPML + added.xml + папка manual/ (RSS-дампы, доклеиваются к подкастам);
  * кэш инвалидируется при изменении входных файлов; при старте с изменёнными
    источниками обход синхронный, при протухшем TTL — фоновый;
  * фильтр по датам, чекбоксы подкастов, добавление по URL;
  * таблица подкастов, таблица эпизодов за месяц (с кнопкой play);
  * в списке последних эпизодов: метка «слушал» и чекбоксы очереди —
    игрок клика play из списка меняет эпизод без перезагрузки страницы,
    по окончании включается случайный отмеченный; выбор нигде не
    хранится, сбрасывается любой перезагрузкой/refresh;
  * лог прослушивания (>10 минут) на диск, показ за месяц; запись
    по ходу прослушивания обновляется, т.е. часы пишутся целиком;
  * автопереход к следующему случайному эпизоду по окончании текущего
    (чекбокс, состояние хранится в localStorage);
  * прокси аудио (обход ERR_BLOCKED_BY_ORB, поддержка Range);
  * retry на 429, последовательная загрузка;
  * production-WSGI waitress.

Запуск:
    pip install flask feedparser requests waitress
    python random-podcast.py
"""

import hashlib
import json
import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from urllib.parse import urlparse, quote as urlquote

import feedparser
import requests
from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, stream_with_context, url_for)

# ------------------------- настройки -------------------------
OPML_PATH  = os.environ.get("OPML_PATH",  "subscriptions.xml")
ADDED_FILE = os.environ.get("ADDED_FILE", "added.xml")
CACHE_FILE = os.environ.get("CACHE_FILE", "episodes_cache.json")
MANUAL_DIR = os.environ.get("MANUAL_DIR", "manual")
CACHE_TTL  = int(os.environ.get("CACHE_TTL", 6 * 3600))

PLAYBACK_LOG       = os.environ.get("PLAYBACK_LOG", "playback_log.json")
PLAYBACK_KEEP_DAYS = int(os.environ.get("PLAYBACK_KEEP_DAYS", 30))
PLAYBACK_MIN_SEC   = int(os.environ.get("PLAYBACK_MIN_SEC", 600))

RECENT_DAYS  = int(os.environ.get("RECENT_DAYS", 30))
RECENT_LIMIT = int(os.environ.get("RECENT_LIMIT", 500))

MAX_WORKERS        = int(os.environ.get("MAX_WORKERS", 1))
RATE_LIMIT_DELAY   = float(os.environ.get("RATE_LIMIT_DELAY", 0.5))
RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", 3))
RETRY_BASE_DELAY   = float(os.environ.get("RETRY_BASE_DELAY", 2.0))

MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", 300))

DEFAULT_FROM = "2000-01-01"
DEFAULT_TO   = date.today().isoformat()

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEBUG_SKIPS = os.environ.get("DEBUG_SKIPS", "1") not in ("0", "false", "False", "")

CACHE_VERSION = 5

HOST    = os.environ.get("HOST", "0.0.0.0")
PORT    = int(os.environ.get("PORT", 5000))
THREADS = int(os.environ.get("THREADS", 8))

MANUAL_EXTS = (".xml", ".rss", ".atom", ".opml")
# -------------------------------------------------------------


# =====================================================================
#  OPML — чтение/запись
# =====================================================================
def parse_opml(path):
    if not path or not os.path.exists(path):
        return []
    try:
        tree = ET.parse(path)
    except Exception as e:
        print(f"[warn] OPML {path}: {e}")
        return []
    feeds = []
    for outline in tree.getroot().iter("outline"):
        url = outline.get("xmlUrl")
        if url:
            title = outline.get("title") or outline.get("text") or url
            feeds.append({"title": title, "url": url.strip()})
    return feeds


def normalize_url(url):
    url = (url or "").strip().strip('"').strip("'")
    url = re.sub(r"^(?:itunes|itms|itpc|itps|itp)://", "https://", url,
                 flags=re.IGNORECASE)
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url
    url = re.sub(r"^(https?://)/+", r"\1", url, flags=re.IGNORECASE)
    return url


def load_all_feeds():
    feeds, seen = [], set()
    for f in parse_opml(OPML_PATH) + parse_opml(ADDED_FILE):
        u = normalize_url(f["url"])
        if u in seen:
            continue
        seen.add(u)
        feeds.append({"title": f["title"], "url": u})
    return feeds


def append_to_added_xml(url, title):
    url = normalize_url(url)
    if os.path.exists(ADDED_FILE):
        tree = ET.parse(ADDED_FILE)
        root = tree.getroot()
        body = root.find("body") or ET.SubElement(root, "body")
    else:
        root = ET.Element("opml", {"version": "1.0"})
        head = ET.SubElement(root, "head")
        ET.SubElement(head, "title").text = "Added subscriptions"
        body = ET.SubElement(root, "body")
        tree = ET.ElementTree(root)

    for outline in body.findall("outline"):
        if (outline.get("xmlUrl") or "").strip() == url:
            return False

    outline = ET.SubElement(body, "outline")
    outline.set("title", title or url)
    outline.set("text",  title or url)
    outline.set("xmlUrl", url)
    outline.set("type", "rss")

    try:
        ET.indent(tree, space="  ")
    except Exception:
        pass
    tree.write(ADDED_FILE, encoding="utf-8", xml_declaration=True)
    return True


# =====================================================================
#  Утилиты
# =====================================================================
AUDIO_EXT = (".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav")


def strip_html(text, max_len=600):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def sources_signature():
    items = []
    for p in (OPML_PATH, ADDED_FILE):
        try:
            st = os.stat(p)
            items.append(f"{p}|{int(st.st_mtime)}|{st.st_size}")
        except OSError:
            items.append(f"{p}|missing")
    if MANUAL_DIR and os.path.isdir(MANUAL_DIR):
        try:
            names = sorted(n for n in os.listdir(MANUAL_DIR)
                           if n.lower().endswith(MANUAL_EXTS))
        except OSError:
            names = []
        for n in names:
            try:
                st = os.stat(os.path.join(MANUAL_DIR, n))
                items.append(f"{n}|{int(st.st_mtime)}|{st.st_size}")
            except OSError:
                items.append(f"{n}|missing")
    else:
        items.append("manual|missing")
    return hashlib.md5("|".join(items).encode("utf-8")).hexdigest()


# =====================================================================
#  RSS — общие помощники
# =====================================================================
def _find_audio(entry):
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or ""
        mime = (enc.get("type") or "").lower()
        if href and (mime.startswith("audio") or href.lower().endswith(AUDIO_EXT)):
            return href
    for link in entry.get("links", []):
        href = link.get("href") or ""
        if link.get("rel") == "enclosure" and href:
            return href
        if href.lower().endswith(AUDIO_EXT):
            return href
    for media in entry.get("media_content", []) or []:
        href = media.get("url")
        if href and (media.get("type", "").startswith("audio")
                     or href.lower().endswith(AUDIO_EXT)):
            return href
    return None


def _published_ts(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return int(datetime(*t[:6], tzinfo=timezone.utc).timestamp())
            except Exception:
                pass
    return None


def _feed_meta(parsed):
    f = parsed.feed
    image = ""
    try:
        if f.get("image") and f["image"].get("href"):
            image = f["image"]["href"]
    except Exception:
        pass
    if not image:
        try:
            image = (f.get("itunes_image") or {}).get("href") or ""
        except Exception:
            pass
    keywords = ""
    try:
        keywords = f.get("itunes_keywords") or ""
    except Exception:
        pass
    if not keywords:
        try:
            keywords = ", ".join(t.get("term", "") for t in (f.get("tags") or [])
                                 if t.get("term"))
        except Exception:
            pass
    return {
        "title":       f.get("title") or "",
        "link":        f.get("link") or "",
        "image":       image,
        "description": strip_html(f.get("description") or ""),
        "summary":     strip_html(f.get("summary") or ""),
        "subtitle":    strip_html(f.get("subtitle") or ""),
        "keywords":    keywords,
    }


def _entry_meta(entry):
    image = ""
    try:
        if entry.get("image") and entry["image"].get("href"):
            image = entry["image"]["href"]
    except Exception:
        pass
    if not image:
        try:
            image = (entry.get("itunes_image") or {}).get("href") or ""
        except Exception:
            pass
    summary = entry.get("summary") or entry.get("description") or ""
    keywords = ""
    try:
        keywords = entry.get("itunes_keywords") or ""
    except Exception:
        pass
    return {
        "image":    image,
        "summary":  strip_html(summary, max_len=800),
        "subtitle": strip_html(entry.get("subtitle") or "", max_len=300),
        "keywords": keywords,
        "duration": entry.get("itunes_duration") or "",
    }


# =====================================================================
#  Сетевой обход одного фида
# =====================================================================
def fetch_feed(feed):
    url = feed["url"]
    content = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        if RATE_LIMIT_DELAY > 0:
            time.sleep(RATE_LIMIT_DELAY)
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/atom+xml, "
                              "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
                    "Accept-Language": "ru,en;q=0.8",
                },
                timeout=30, allow_redirects=True,
            )
        except requests.RequestException as e:
            print(f"[warn] {feed['title']}: сетевая ошибка "
                  f"({attempt}/{RETRY_MAX_ATTEMPTS}): {e}")
            if attempt < RETRY_MAX_ATTEMPTS:
                time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            return [], {}
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            wait = (int(ra) if ra and ra.isdigit()
                    else RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            print(f"[429]  {feed['title']}: жду {wait:.1f}с "
                  f"({attempt}/{RETRY_MAX_ATTEMPTS})")
            if attempt < RETRY_MAX_ATTEMPTS:
                time.sleep(wait)
                continue
            return [], {}
        if r.status_code >= 400:
            print(f"[warn] {feed['title']}: HTTP {r.status_code} для {url}")
            return [], {}
        content = r.content
        break
    if content is None:
        return [], {}

    parsed = feedparser.parse(content)
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        print(f"[warn] {feed['title']}: фид не распарсился "
              f"({getattr(parsed, 'bozo_exception', '?')})")
        return [], {}
    if not parsed.entries:
        print(f"[info] {feed['title']}: пустой фид")
        return [], {}

    pmeta = _feed_meta(parsed)
    pmeta["feed_url"] = url

    episodes = []
    sk_a = sk_d = 0
    for entry in parsed.entries[:MAX_PER_FEED]:
        audio = _find_audio(entry)
        ts = _published_ts(entry)
        if not audio:
            sk_a += 1
            if DEBUG_SKIPS:
                print(f"[skip] {feed['title']}: нет аудио в "
                      f"«{entry.get('title','?')}»")
            continue
        if ts is None:
            sk_d += 1
            if DEBUG_SKIPS:
                print(f"[skip] {feed['title']}: нет даты у "
                      f"«{entry.get('title','?')}» "
                      f"(published={entry.get('published')!r}, "
                      f"updated={entry.get('updated')!r})")
            continue
        emeta = _entry_meta(entry)
        episodes.append({
            "podcast":      feed["title"],
            "podcast_link": pmeta.get("link", ""),
            "feed_url":     url,
            "title":        entry.get("title", "Без названия"),
            "audio":        audio,
            "link":         entry.get("link", ""),
            "published":    entry.get("published", "") or entry.get("updated", ""),
            "ts":           ts,
            "image":        emeta["image"],
            "summary":      emeta["summary"],
            "subtitle":     emeta["subtitle"],
            "keywords":     emeta["keywords"],
            "duration":     emeta["duration"],
            "manual_file":  "",
        })
    print(f"[ok]   {feed['title']}: {len(episodes)}/{len(parsed.entries)} "
          f"(без аудио: {sk_a}, без даты: {sk_d})")
    return episodes, pmeta


# =====================================================================
#  Ручные файлы (manual/)
# =====================================================================
def _feed_keys(feed):
    keys = set()
    url = (feed.get("url") or "").strip()
    if url:
        nu = normalize_url(url)
        keys.add(nu.lower())
        try:
            p = urlparse(nu)
            keys.add(f"{p.scheme}://{p.netloc}{p.path}".lower())
            for s in [x for x in p.path.split("/") if x]:
                keys.add(s.lower())
                stem = os.path.splitext(s)[0].lower()
                if stem and len(stem) > 2:
                    keys.add(stem)
        except Exception:
            pass
    t = (feed.get("title") or "").strip()
    if t:
        keys.add(t.lower())
    return keys


def _manual_keys(name, parsed):
    keys = set()
    stem = os.path.splitext(name)[0]
    if stem:
        keys.add(stem.lower())
    try:
        link = (parsed.feed.get("link") or "").strip()
        if link:
            keys.add(link.lower())
            keys.add(normalize_url(link).lower())
            try:
                p = urlparse(normalize_url(link))
                for s in [x for x in p.path.split("/") if x]:
                    keys.add(s.lower())
                    st = os.path.splitext(s)[0].lower()
                    if st and len(st) > 2:
                        keys.add(st)
            except Exception:
                pass
    except Exception:
        pass
    try:
        t = (parsed.feed.get("title") or "").strip()
        if t:
            keys.add(t.lower())
    except Exception:
        pass
    return keys


def load_manual_files():
    if not MANUAL_DIR or not os.path.isdir(MANUAL_DIR):
        print(f"[manual] папка {MANUAL_DIR!r} не найдена")
        return {}
    by_key = {}
    files = sorted(os.listdir(MANUAL_DIR))
    n_files = n_eps = 0
    for name in files:
        if not name.lower().endswith(MANUAL_EXTS):
            continue
        path = os.path.join(MANUAL_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                content = f.read()
        except Exception as e:
            print(f"[manual] {name}: не прочитан ({e})")
            continue
        parsed = feedparser.parse(content)
        if not parsed.entries:
            print(f"[manual] {name}: нет <item>")
            continue
        try:
            ch_title = parsed.feed.get("title") or ""
            ch_link  = (parsed.feed.get("link") or "").strip()
        except Exception:
            ch_title, ch_link = "", ""
        try:
            default_ts = int(os.path.getmtime(path))
        except Exception:
            default_ts = int(time.time())

        eps = []
        for entry in parsed.entries:
            audio = _find_audio(entry)
            if not audio:
                continue
            ts = _published_ts(entry) or default_ts
            emeta = _entry_meta(entry)
            eps.append({
                "podcast":      ch_title or os.path.splitext(name)[0],
                "podcast_link": ch_link,
                "feed_url":     "",   # заполним в _fetch_and_merge
                "title":        entry.get("title", "Без названия"),
                "audio":        audio,
                "link":         entry.get("link", ""),
                "published":    entry.get("published", "") or entry.get("updated", "")
                                 or "(manual)",
                "ts":           ts,
                "image":        emeta["image"],
                "summary":      emeta["summary"],
                "subtitle":     emeta["subtitle"],
                "keywords":     emeta["keywords"],
                "duration":     emeta["duration"],
                "manual_file":  name,
                "from_manual":  True,
            })
        if not eps:
            continue
        keys = _manual_keys(name, parsed)
        for k in keys:
            by_key.setdefault(k, []).extend(eps)
        n_files += 1
        n_eps += len(eps)
        print(f"[manual] {name}: {len(eps)} эпизодов")
    print(f"[manual] итого: {n_files} файлов, {n_eps} эпизодов")
    return by_key


# =====================================================================
#  Кэш + фоновое обновление
# =====================================================================
_refresh_lock    = threading.Lock()
_refresh_running = threading.Event()


def load_cache_stale():
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    print(f"[cache] загружено {len(data.get('episodes', []))} эпизодов "
          f"из {CACHE_FILE}")
    return data


def _needs_refresh(cached):
    if cached is None:
        return True
    try:
        if time.time() - os.path.getmtime(CACHE_FILE) > CACHE_TTL:
            return True
    except OSError:
        return True
    if cached.get("sources_sig") != sources_signature():
        return True
    return False


def save_cache(episodes, podcasts):
    data = {
        "version":     CACHE_VERSION,
        "sources_sig": sources_signature(),
        "episodes":    episodes,
        "podcasts":    podcasts,
    }
    tmp = CACHE_FILE + ".tmp"
    print(f"[cache] сохраняю {len(episodes)} эпизодов, "
          f"{len(podcasts)} подкастов → {CACHE_FILE}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)
    try:
        sz = os.path.getsize(CACHE_FILE)
    except OSError:
        sz = -1
    print(f"[cache] записано ({sz} байт)")


def _do_refresh():
    feeds = load_all_feeds()
    print(f"[refresh] {len(feeds)} фидов")
    episodes, podcasts = build_all(feeds)
    save_cache(episodes, podcasts)
    print(f"[refresh] готово: {len(episodes)} эпизодов, "
          f"{len(podcasts)} подкастов")
    return {"episodes": episodes, "podcasts": podcasts}


def _start_bg_refresh():
    if _refresh_running.is_set():
        return
    _refresh_running.set()
    threading.Thread(target=_bg_refresh, daemon=True).start()


def _bg_refresh():
    try:
        print("[bg] фоновое обновление начато")
        _do_refresh()
    except Exception as e:
        print(f"[bg] ошибка: {e}")
    finally:
        _refresh_running.clear()


def get_data(force_refresh=False):
    if force_refresh:
        return _do_refresh()

    cached = load_cache_stale()
    if cached is None:
        print("[cold] кэша нет — собираю (это займёт время)…")
        return _do_refresh()

    if _needs_refresh(cached):
        _start_bg_refresh()

    return cached


# =====================================================================
#  Сборка базы
# =====================================================================
def _fetch_and_merge(feed, manual_by_key):
    eps, meta = fetch_feed(feed)

    fkeys = _feed_keys(feed)
    manual_eps = []
    matched = set()
    for k in fkeys:
        if k in manual_by_key:
            manual_eps.extend(manual_by_key[k])
            matched.add(k)

    if manual_eps:
        print(f"[manual] {feed['title']}: +{len(manual_eps)} "
              f"(ключи: {sorted(matched)})")

    extra = []
    for mep in manual_eps:
        mep2 = dict(mep)
        mep2["feed_url"] = feed["url"]
        if meta:
            if meta.get("title"):
                mep2["podcast"] = meta["title"]
            if meta.get("link"):
                mep2["podcast_link"] = meta["link"]
        extra.append(mep2)

    # Локальный дедуп внутри этого фида по audio (не затрагивает другие фиды)
    local_seen, local_unique = set(), []
    for ep in eps + extra:
        if ep["audio"] in local_seen:
            continue
        local_seen.add(ep["audio"])
        local_unique.append(ep)

    return local_unique, meta


def build_all(feeds):
    manual_by_key = load_manual_files()
    episodes, podcasts = [], {}

    if MAX_WORKERS <= 1:
        for feed in feeds:
            try:
                eps, meta = _fetch_and_merge(feed, manual_by_key)
            except Exception as e:
                print(f"[warn] {feed['title']}: {e}")
                continue
            episodes.extend(eps)
            if meta:
                podcasts[feed["url"]] = meta
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_fetch_and_merge, f, manual_by_key): f
                       for f in feeds}
            for fut in as_completed(futures):
                feed = futures[fut]
                try:
                    eps, meta = fut.result()
                except Exception as e:
                    print(f"[warn] {feed['title']}: {e}")
                    continue
                episodes.extend(eps)
                if meta:
                    podcasts[feed["url"]] = meta

    # Глобальный дедуп по паре (feed_url, audio), а не просто по audio,
    # чтобы manual-эпизоды одного фида не «съедались» эпизодами другого.
    seen, unique = set(), []
    for ep in episodes:
        key = (ep.get("feed_url", ""), ep["audio"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(ep)

    return unique, podcasts


# =====================================================================
#  Фильтр / статистика
# =====================================================================
def parse_date(s, fallback):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return fallback


def filter_by_date(episodes, d_from, d_to):
    ts_from = int(datetime(d_from.year, d_from.month, d_from.day,
                           tzinfo=timezone.utc).timestamp())
    ts_to = int(datetime(d_to.year, d_to.month, d_to.day,
                         tzinfo=timezone.utc).timestamp()) + 86399
    return [e for e in episodes if ts_from <= e["ts"] <= ts_to]


def podcast_stats(episodes, podcasts):
    by_url = {}
    for e in episodes:
        url = e.get("feed_url", "")
        s = by_url.setdefault(url, {
            "feed_url": url, "title": e["podcast"],
            "link": e.get("podcast_link", ""), "count": 0, "manual": 0,
            "first_ts": e["ts"], "last_ts": e["ts"],
        })
        s["count"] += 1
        if e.get("from_manual"):
            s["manual"] += 1
        if e["ts"] < s["first_ts"]:
            s["first_ts"] = e["ts"]
        if e["ts"] > s["last_ts"]:
            s["last_ts"] = e["ts"]
        if not s["link"] and e.get("podcast_link"):
            s["link"] = e["podcast_link"]
    for url, m in podcasts.items():
        if url in by_url:
            for k in ("image", "description", "summary", "subtitle", "keywords"):
                by_url[url][k] = m.get(k, "")
            if not by_url[url]["link"] and m.get("link"):
                by_url[url]["link"] = m["link"]
    out = []
    for s in by_url.values():
        s["first_date"] = datetime.fromtimestamp(
            s["first_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        s["last_date"] = datetime.fromtimestamp(
            s["last_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        out.append(s)
    out.sort(key=lambda x: x["title"].lower())
    return out


def played_audios_view():
    """Множество audio уже прослушанных (>= MIN_SEC) эпизодов."""
    return {e.get("audio", "")
            for e in _prune_playback(load_playback_log())
            if int(e.get("listened_seconds", 0)) >= PLAYBACK_MIN_SEC}


def recent_episodes_view(episodes, days=RECENT_DAYS, limit=RECENT_LIMIT,
                         played=None):
    played = played or set()
    cutoff = int(time.time()) - days * 86400
    out = []
    for e in episodes:
        if e["ts"] < cutoff:
            continue
        e2 = dict(e)
        e2["date_str"] = datetime.fromtimestamp(
            e["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        e2["played"] = e["audio"] in played
        out.append(e2)
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]


# =====================================================================
#  Лог прослушивания
# =====================================================================
_playback_lock = threading.Lock()


def load_playback_log():
    if not os.path.exists(PLAYBACK_LOG):
        return []
    try:
        with open(PLAYBACK_LOG, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("episodes", []) or []
    except Exception:
        return []


def _prune_playback(entries):
    cutoff = int(time.time()) - PLAYBACK_KEEP_DAYS * 86400
    return [e for e in entries if int(e.get("start_ts", 0)) >= cutoff]


def save_playback_log(entries):
    entries = _prune_playback(entries)
    tmp = PLAYBACK_LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"episodes": entries}, f, ensure_ascii=False)
    os.replace(tmp, PLAYBACK_LOG)


def append_playback(entry):
    """Добавляет запись или обновляет существующую (тот же audio + start_ts):
    растут end_ts и listened_seconds, пока эпизод играет."""
    with _playback_lock:
        entries = load_playback_log()
        key = (entry.get("audio", ""), int(entry.get("start_ts", 0)))
        new_end = int(entry.get("end_ts", 0))
        new_secs = int(entry.get("listened_seconds", 0))
        for i, e in enumerate(entries):
            if (e.get("audio", ""), int(e.get("start_ts", 0))) == key:
                e["end_ts"] = max(int(e.get("end_ts", 0)), new_end)
                e["listened_seconds"] = max(
                    int(e.get("listened_seconds", 0)), new_secs)
                save_playback_log(entries)
                return True
        entries.append(entry)
        save_playback_log(entries)
        return True


def playback_view():
    entries = _prune_playback(load_playback_log())
    out = []
    for e in entries:
        e2 = dict(e)
        try:
            e2["start_str"] = datetime.fromtimestamp(
                e["start_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            e2["end_str"] = datetime.fromtimestamp(
                e["end_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            secs = int(e.get("listened_seconds", 0))
            e2["listened_str"] = f"{secs // 60} мин"
        except Exception:
            e2["start_str"] = e2["end_str"] = e2["listened_str"] = ""
        out.append(e2)
    out.sort(key=lambda x: int(x.get("start_ts", 0)), reverse=True)
    return out


# =====================================================================
#  Flask
# =====================================================================
app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml"
      href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E%F0%9F%8E%A7%3C/text%3E%3C/svg%3E">
<title>Случайный подкаст</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 1160px;
         margin: 40px auto; padding: 0 20px; color: #111; }
  h1   { font-size: 1rem; color: #666; font-weight: 500; margin: 0 0 4px; }
  h2   { font-size: 1.35rem; margin: 0 0 4px; line-height: 1.3; }
  .meta { color: #777; font-size: .9rem; margin: 0 0 16px; }
  audio { width: 100%; margin: 12px 0 12px; }
  .autoplay-row { margin: 0 0 12px; font-size: .9rem; color: #555; }
  .autoplay-row label { user-select: none; cursor: pointer; }
  a.btn, button.btn { display: inline-block; padding: 10px 16px; background: #111;
          color: #fff; border-radius: 8px; text-decoration: none;
          margin: 0 8px 8px 0; font-size: .95rem; border: none; cursor: pointer; }
  a.btn.secondary, button.btn.secondary { background: #eee; color: #111; }
  a.btn.small { padding: 3px 10px; font-size: .85rem; margin: 0; }

  form.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: end;
                 margin: 16px 0; padding: 12px; background: #f6f6f6;
                 border-radius: 10px; }
  form.filters label { display: flex; flex-direction: column; font-size: .8rem;
                       color: #555; }
  form.filters input { padding: 6px 8px; border: 1px solid #ccc;
                       border-radius: 6px; font-size: .95rem; }
  .stats { font-size: .85rem; color: #666; margin-left: auto; }

  form.add { display: flex; gap: 8px; margin: 16px 0; padding: 12px;
             background: #eef7ff; border-radius: 10px; flex-wrap: wrap; }
  form.add input[type=text] { flex: 1; min-width: 260px; padding: 8px 10px;
             border: 1px solid #a9c8e0; border-radius: 6px; font-size: .95rem; }
  form.add button { padding: 8px 16px; background: #0a66c2; color: #fff;
             border: none; border-radius: 6px; cursor: pointer; font-size: .95rem; }

  .empty { padding: 20px; background: #fff3f3; border: 1px solid #f0c0c0;
           border-radius: 10px; color: #a00; }
  details { margin: 20px 0 0; }
  details > summary { cursor: pointer; font-weight: 600; padding: 8px 0;
                      font-size: 1rem; }
  .table-wrap { max-height: 500px; overflow-y: auto;
                border: 1px solid #eee; border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee;
           vertical-align: top; }
  th { background: #fafafa; font-weight: 600; color: #444;
       position: sticky; top: 0; }
  td.num { text-align: right; white-space: nowrap; }
  td a { color: #0a66c2; word-break: break-all; }
  .cb { width: 28px; }
  footer { margin-top: 24px; color: #999; font-size: .8rem; }
  .badge-manual { display: inline-block; padding: 1px 6px; margin-left: 4px;
                  background: #e7f5e9; color: #256029; border-radius: 6px;
                  font-size: .72rem; font-weight: 600; vertical-align: middle; }
  .badge-bg { display: inline-block; padding: 1px 8px; margin-left: 8px;
              background: #fff3c4; color: #7a5900; border-radius: 6px;
              font-size: .75rem; }
  .badge-played { display: inline-block; padding: 1px 6px; margin-left: 4px;
                  background: #dcefdc; color: #2c5f2d; border-radius: 6px;
                  font-size: .72rem; font-weight: 600; vertical-align: middle; }
  tr.played td { color: #999; }
  tr.played td a.btn { background: #ddd; color: #555; }

  .tt { position: relative; display: inline-block; cursor: help;
        border-bottom: 1px dotted #bbb; }
  /* Скрытый источник содержимого тултипа внутри ячейки */
  .tt .tip { display: none; }
  /* Плавающий тултип: один на страницу, overlay поверх всего
     (не обрезается overflow у .table-wrap) */
  .tip-float {
    position: fixed; display: none; z-index: 1000; pointer-events: none;
    background: #fff; border: 1px solid #ccc; border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,.18);
    padding: 10px 12px; width: 360px; max-width: 90vw;
    font-size: .82rem; font-weight: 400; color: #333;
    line-height: 1.35; white-space: normal; text-align: left;
  }
  .tip-float img { max-width: 100%; max-height: 140px; border-radius: 6px;
                   display: block; margin-bottom: 6px; }
  .tip-float .tip-title { font-weight: 600; margin-bottom: 4px; }
  .tip-float .tip-kv { color: #555; margin-top: 4px; }
  .tip-float .tip-kv b { color: #333; }
</style>
</head>
<body>

<form class="filters" method="get" action="{{ url_for('index') }}"
      onsubmit="return applyFilters(this);">
  <label>С
    <input type="date" name="from" value="{{ d_from }}" min="1990-01-01">
  </label>
  <label>До
    <input type="date" name="to" value="{{ d_to }}" min="1990-01-01">
  </label>
  <button type="submit" class="btn">Применить</button>
  <span class="stats">
    Доступно эпизодов: <b>{{ total_filtered }}</b> из {{ total_all }}
    {% if selected_feeds %}(выбрано: {{ selected_feeds|length }}){% endif %}
    {% if bg_running %}<span class="badge-bg">фон. обновление…</span>{% endif %}
  </span>
</form>

<form class="add" method="post" action="{{ url_for('add') }}">
  <input type="text" name="url"
         placeholder="URL RSS подкаста, например promodj.com/xxx/podcast.xml"
         required>
  <button type="submit">Добавить подкаст</button>
</form>

{% if ep %}
  <h1>
    <span class="tt"><span id="ep-podcast">{{ ep.podcast }}</span>
      <div class="tip" id="ep-tip">
        {% if ep.image %}<img src="{{ ep.image }}" alt="">{% endif %}
        <div class="tip-title">{{ ep.title }}</div>
        {% if ep.summary %}<div>{{ ep.summary }}</div>{% endif %}
        {% if ep.subtitle %}<div class="tip-kv"><b>Subtitle:</b> {{ ep.subtitle }}</div>{% endif %}
        {% if ep.duration %}<div class="tip-kv"><b>Длительность:</b> {{ ep.duration }}</div>{% endif %}
        {% if ep.keywords %}<div class="tip-kv"><b>Ключевые слова:</b> {{ ep.keywords }}</div>{% endif %}
        {% if ep.manual_file %}<div class="tip-kv"><b>Источник:</b> manual/{{ ep.manual_file }}</div>{% endif %}
        {% if ep.link %}<div class="tip-kv"><a href="{{ ep.link }}" target="_blank" rel="noopener">Страница эпизода</a></div>{% endif %}
      </div>
    </span>
  </h1>
  <h2><span id="ep-title">{{ ep.title }}</span>
    <span class="badge-manual" id="ep-manual-badge" style="{{ '' if ep.manual_file else 'display:none' }}">manual</span>
    <span class="badge-bg" id="ep-manual-choice" style="{{ '' if play_url else 'display:none' }}">выбран вручную</span>
  </h2>
  <p class="meta" id="ep-published">{{ ep.published }}</p>

  <audio controls autoplay preload="none" data-track="1"
         data-audio="{{ ep.audio }}"
         data-podcast="{{ ep.podcast }}"
         data-title="{{ ep.title }}"
         data-link="{{ ep.link }}"
         data-from-play="{{ '1' if play_url else '0' }}"
         src="{{ url_for('proxy_audio', url=ep.audio) }}"></audio>

  <p class="autoplay-row">
    <label>
      <input type="checkbox" id="autoplay-next" checked>
      Автоматически включать следующий эпизод
    </label>
  </p>

  <p>
    <a class="btn" href="#" onclick="return nextRandom();">Ещё случайный</a>
    <a class="btn secondary" href="{{ url_for('refresh') }}">Обновить ленты</a>
    {% if ep.link %}<a class="btn secondary" href="{{ ep.link }}" target="_blank" rel="noopener">Страница эпизода</a>{% endif %}
  </p>
{% else %}
  <div class="empty">
    В выбранном диапазоне эпизодов нет. Расширьте даты, снимите чекбоксы
    или нажмите «Обновить ленты».
  </div>
  <p><a class="btn secondary" href="{{ url_for('refresh') }}">Обновить ленты</a></p>
{% endif %}

<details class="recent" open>
  <summary>Эпизоды за последние {{ recent_days }} дней ({{ recent|length }})</summary>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="cb" title="Отметьте — в очередь случайного автопроигрывания">Q</th>
          <th style="width: 130px;">Дата</th>
          <th style="width: 200px;">Подкаст</th>
          <th>Эпизод</th>
          <th style="width: 60px;"></th>
        </tr>
      </thead>
      <tbody>
        {% for e in recent %}
        <tr{% if e.played %} class="played"{% endif %}
           data-audio="{{ e.audio }}"
           data-podcast="{{ e.podcast }}"
           data-title="{{ e.title }}"
           data-link="{{ e.link }}"
           data-published="{{ e.published }}"
           data-manual="{{ e.manual_file }}">
          <td class="cb">
            <input type="checkbox" class="queue-cb" data-audio="{{ e.audio }}">
          </td>
          <td>{{ e.date_str }}</td>
          <td>{{ e.podcast }}</td>
          <td>
            <span class="tt">{{ e.title }}
              {% if e.played %}<span class="badge-played">✔ слушал</span>{% endif %}
              <div class="tip">
                {% if e.image %}<img src="{{ e.image }}" alt="">{% endif %}
                {% if e.summary %}<div>{{ e.summary }}</div>{% endif %}
                {% if e.duration %}<div class="tip-kv"><b>Длительность:</b> {{ e.duration }}</div>{% endif %}
                {% if e.manual_file %}<div class="tip-kv"><b>Источник:</b> manual/{{ e.manual_file }}</div>{% endif %}
              </div>
            </span>
          </td>
          <td>
            <a class="btn small play-ep" href="?play={{ e.audio|urlencode }}{{ preserve_qs|safe }}">▶</a>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</details>

<details class="podcasts">
  <summary>Все подкасты ({{ podcasts|length }}) — отметьте, из каких брать случайные</summary>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="cb"></th>
          <th>Название</th>
          <th>Сайт</th>
          <th class="num">Эпизодов</th>
          <th>Первый</th>
          <th>Последний</th>
        </tr>
      </thead>
      <tbody>
        {% for p in podcasts %}
        <tr>
          <td class="cb">
            <input type="checkbox" name="p" value="{{ p.feed_url }}"
                   form="chkform"
                   {% if p.feed_url in selected_feeds %}checked{% endif %}>
          </td>
          <td>
            <span class="tt">{{ p.title }}
              <div class="tip">
                {% if p.image %}<img src="{{ p.image }}" alt="">{% endif %}
                {% if p.link %}<div class="tip-kv"><a href="{{ p.link }}" target="_blank" rel="noopener">{{ p.link }}</a></div>{% endif %}
                {% if p.description %}<div>{{ p.description }}</div>{% endif %}
                {% if p.subtitle %}<div class="tip-kv"><b>Subtitle:</b> {{ p.subtitle }}</div>{% endif %}
                {% if p.summary and p.summary != p.description %}<div class="tip-kv"><b>Summary:</b> {{ p.summary }}</div>{% endif %}
                {% if p.keywords %}<div class="tip-kv"><b>Ключевые слова:</b> {{ p.keywords }}</div>{% endif %}
                {% if p.manual %}<div class="tip-kv"><b>Из manual:</b> {{ p.manual }}</div>{% endif %}
              </div>
            </span>
            {% if p.manual %}<span class="badge-manual">+{{ p.manual }} manual</span>{% endif %}
          </td>
          <td>{% if p.link %}<a href="{{ p.link }}" target="_blank" rel="noopener">{{ p.link }}</a>{% else %}—{% endif %}</td>
          <td class="num">{{ p.count }}</td>
          <td>{{ p.first_date }}</td>
          <td>{{ p.last_date }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</details>

<details class="playback">
  <summary>Прослушано за {{ playback_keep_days }} дней ({{ playback_log|length }})</summary>
  {% if playback_log %}
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Подкаст</th>
          <th>Эпизод</th>
          <th>Начало</th>
          <th>Конец</th>
          <th>Слушал</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for r in playback_log %}
        <tr>
          <td>{{ r.podcast }}</td>
          <td>{{ r.title }}</td>
          <td>{{ r.start_str }}</td>
          <td>{{ r.end_str }}</td>
          <td>{{ r.listened_str }}</td>
          <td>{% if r.link %}<a href="{{ r.link }}" target="_blank" rel="noopener">→</a>{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p style="color: #888;">Пока ничего не прослушано на 10+ минут.</p>
  {% endif %}
</details>

<form id="chkform" onsubmit="return false;"></form>

<footer>
  База: {{ total_all }} эпизодов · диапазон {{ d_from }} → {{ d_to }}
</footer>

<script>
function currentUrl() {
  const params = new URLSearchParams();
  const from = document.querySelector('input[name="from"]').value;
  const to   = document.querySelector('input[name="to"]').value;
  if (from) params.set('from', from);
  if (to)   params.set('to', to);
  document.querySelectorAll('input[name="p"]:checked').forEach(cb => {
    params.append('p', cb.value);
  });
  return '/?' + params.toString();
}
function applyFilters(form) { window.location.href = currentUrl(); return false; }
function nextRandom()       { window.location.href = currentUrl(); return false; }

/* Тултипы: один плавающий элемент поверх страницы.
   Содержимое берётся из скрытого .tip внутри наведённого .tt,
   поэтому обрезание overflow у .table-wrap не страшно. */
(function () {
  const GAP = 20;
  let floatEl = null, activeTip = null;

  function ensureFloat() {
    if (floatEl) return;
    floatEl = document.createElement('div');
    floatEl.className = 'tip tip-float';
    document.body.appendChild(floatEl);
  }

  function hide() {
    if (floatEl) floatEl.style.display = 'none';
    activeTip = null;
  }

  function show(tt) {
    const tip = tt.querySelector('.tip');
    if (!tip) return;
    ensureFloat();
    if (activeTip !== tip) {
      floatEl.innerHTML = tip.innerHTML;
      activeTip = tip;
    }
    /* замер при скрытии — без мигания */
    floatEl.style.visibility = 'hidden';
    floatEl.style.display = 'block';
    const w = floatEl.offsetWidth, h = floatEl.offsetHeight;
    const r = tt.getBoundingClientRect();
    const winH = window.innerHeight, winW = window.innerWidth;
    let top = r.bottom + 8;
    if (winH - r.bottom - GAP < h && r.top - GAP > h) top = r.top - h - 8;
    let left = r.left;
    if (left + w > winW - GAP) left = winW - GAP - w;
    if (left < GAP) left = GAP;
    floatEl.style.top = top + 'px';
    floatEl.style.left = left + 'px';
    floatEl.style.visibility = 'visible';
  }

  document.addEventListener('mouseover', e => {
    const tt = e.target.closest && e.target.closest('.tt');
    if (tt) show(tt);
  });
  document.addEventListener('mouseout', e => {
    if (!e.target.closest) return;
    const tt = e.target.closest('.tt');
    if (tt && !(e.relatedTarget && e.relatedTarget.closest &&
                e.relatedTarget.closest('.tt'))) hide();
  });
  /* прокрутка любого контейнера — прятать, т.к. fixed привязан к вьюпорту */
  document.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
})();

/* Лог прослушивания + автопереход к следующему эпизоду */
(function () {
  const audio = document.querySelector('audio[data-track="1"]');
  if (!audio) return;

  const MIN_SEC = {{ playback_min_sec }};
  let meta = {
    audio:    audio.dataset.audio    || '',
    podcast:  audio.dataset.podcast  || '',
    title:    audio.dataset.title    || '',
    link:     audio.dataset.link     || '',
    fromPlay: audio.dataset.fromPlay === '1',
  };

  /* --- Лог прослушивания --- */
  let startTs = null, endTs = null;
  let listened = 0, lastTick = null;

  if (meta.audio) {
    setInterval(function () {
      const now = Date.now();
      if (lastTick && !audio.paused && !audio.ended) {
        const dt = (now - lastTick) / 1000;
        if (dt > 0 && dt < 5) listened += dt;
      }
      lastTick = now;
    }, 1000);

    audio.addEventListener('play', function () {
      const now = Math.floor(Date.now() / 1000);
      if (startTs === null) startTs = now;
      endTs = now;
      lastTick = Date.now();
    });
    audio.addEventListener('timeupdate', function () {
      if (!audio.paused && !audio.ended) endTs = Math.floor(Date.now() / 1000);
    });
  }

  function sendLog() {
    if (!meta.audio) return;
    if (listened < MIN_SEC) return;
    if (startTs === null) return;
    if (endTs === null || endTs < startTs) {
      endTs = Math.floor(Date.now() / 1000);
    }
    const body = JSON.stringify({
      audio:   meta.audio,
      podcast: meta.podcast,
      title:   meta.title,
      link:    meta.link,
      start_ts: startTs,
      end_ts:   endTs || Math.floor(Date.now() / 1000),
      listened_seconds: Math.round(listened),
    });
    try {
      navigator.sendBeacon('/log_playback',
        new Blob([body], { type: 'application/json' }));
    } catch (e) {
      fetch('/log_playback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: body, keepalive: true,
      });
    }
  }

  window.addEventListener('beforeunload', sendLog);
  setInterval(sendLog, 30000);

  /* --- Очередь = живые чекбоксы в DOM. Нигде не хранится:
         любая перезагрузка страницы сбрасывает выбор. --- */
  function queueBoxes(excludeAudio) {
    if (!document.querySelector('input.queue-cb')) return [];
    return Array.from(document.querySelectorAll('tr[data-audio]'))
      .filter(tr => tr.dataset.audio !== excludeAudio &&
                    tr.querySelector('input.queue-cb:checked'));
  }

  const logState = {
    reset: function () {
      startTs = null; endTs = null; lastTick = null; listened = 0;
    },
  };

  function swapEpisode(tr) {
    sendLog(); /* дособрать предыдущий эпизод, если дослушан до порога */
    const data = tr.dataset;
    audio.dataset.audio    = data.audio || '';
    audio.dataset.podcast  = data.podcast || '';
    audio.dataset.title    = data.title || '';
    audio.dataset.link     = data.link || '';
    audio.dataset.fromPlay = '1';
    meta = {
      audio:   audio.dataset.audio,
      podcast: audio.dataset.podcast,
      title:   audio.dataset.title,
      link:    audio.dataset.link,
      fromPlay: true,
    };

    const epTitle = document.getElementById('ep-title');
    if (epTitle) epTitle.textContent = meta.title;
    const epPodcast = document.getElementById('ep-podcast');
    if (epPodcast) epPodcast.textContent = meta.podcast;
    const epPublished = document.getElementById('ep-published');
    if (epPublished) epPublished.textContent = data.published || '';
    const rowTip = tr.querySelector('.tip');
    const epTip = document.getElementById('ep-tip');
    if (rowTip && epTip) epTip.innerHTML = rowTip.innerHTML;
    const manualBadge = document.getElementById('ep-manual-badge');
    if (manualBadge) manualBadge.style.display = data.manual ? '' : 'none';
    const choiceBadge = document.getElementById('ep-manual-choice');
    if (choiceBadge) choiceBadge.style.display = '';

    nextScheduled = false;
    logState.reset();
    audio.src = '/audio?url=' + encodeURIComponent(meta.audio);
    audio.play().catch(e => console.warn('play() отклонён браузером:', e));
  }

  /* Кнопка ▶ из списка: переключение эпизода на месте (без рефреша) —
     ТОЛЬКО если в списке есть отмеченные галочки (есть очередь для
     продолжения). Во всех остальных случаях — обычная навигация по
     href (?play=...), т.е. рефреш страницы. */
  document.querySelectorAll('a.play-ep').forEach(a => {
    a.addEventListener('click', function (e) {
      const tr = a.closest && a.closest('tr[data-audio]');
      if (!tr) return;
      const hasQueue = queueBoxes(tr.dataset.audio).length > 0;
      if (!hasQueue || tr.dataset.audio === meta.audio) return;
      /* обычный href остаётся fallback-ом на случай JS-ошибки */
      e.preventDefault();
      swapEpisode(tr);
    });
  });

  /* --- Автопереход к следующему эпизоду --- */
  const apCb = document.getElementById('autoplay-next');
  if (apCb) {
    const saved = localStorage.getItem('autoplay-next');
    if (saved === '0') apCb.checked = false;
    apCb.addEventListener('change', () => {
      localStorage.setItem('autoplay-next', apCb.checked ? '1' : '0');
    });
  }

  let nextScheduled = false;
  audio.addEventListener('ended', function () {
    sendLog();
    if (nextScheduled) return;
    nextScheduled = true;
    /* небольшая задержка, чтобы sendBeacon успел уйти */
    setTimeout(function () {
      /* Продолжение очереди (галочки в списке «последние» + запуск
         кнопкой play оттуда) — случайный отмеченный, переключение
         на месте. Все остальные случаи — рефреш страницы. */
      if (meta.fromPlay) {
        const boxes = queueBoxes(meta.audio);
        if (boxes.length) {
          nextScheduled = false;
          const tr = boxes[Math.floor(Math.random() * boxes.length)];
          swapEpisode(tr);
          return;
        }
      }
      if (!(apCb && apCb.checked)) return;
      window.location.href = currentUrl();
    }, 1000);
  });
})();
</script>

</body>
</html>
"""


# ------------------------- маршруты --------------------------
def _build_preserve_qs():
    parts = []
    for k, vals in request.args.lists():
        if k == "play":
            continue
        for v in vals:
            parts.append(f"{k}={urlquote(v)}")
    return ("&" + "&".join(parts)) if parts else ""


@app.route("/")
def index():
    data = get_data()
    episodes = data["episodes"]
    podcasts_meta = data["podcasts"]

    if not episodes:
        return ("Не найдено ни одного эпизода с аудио и датой. "
                "Проверьте OPML/added.xml/manual и логи (DEBUG_SKIPS=1)."), 500

    d_from = parse_date(request.args.get("from"), date.fromisoformat(DEFAULT_FROM))
    d_to   = parse_date(request.args.get("to"),   date.fromisoformat(DEFAULT_TO))
    if d_from > d_to:
        d_from, d_to = d_to, d_from

    selected_feeds = set(request.args.getlist("p"))
    play_url = request.args.get("play")

    if play_url:
        ep = next((e for e in episodes if e.get("audio") == play_url), None)
        if ep is None:
            play_url = None

    filtered = filter_by_date(episodes, d_from, d_to)

    if not play_url:
        if selected_feeds:
            filtered = [e for e in filtered
                        if e.get("feed_url") in selected_feeds]
        ep = random.choice(filtered) if filtered else None

    return render_template_string(
        PAGE,
        ep=ep,
        play_url=play_url,
        total_all=len(episodes),
        total_filtered=len(filtered),
        podcasts=podcast_stats(episodes, podcasts_meta),
        recent=recent_episodes_view(
            episodes, played=played_audios_view()),
        recent_days=RECENT_DAYS,
        playback_log=playback_view(),
        playback_keep_days=PLAYBACK_KEEP_DAYS,
        playback_min_sec=PLAYBACK_MIN_SEC,
        selected_feeds=selected_feeds,
        preserve_qs=_build_preserve_qs(),
        bg_running=_refresh_running.is_set(),
        d_from=d_from.isoformat(),
        d_to=d_to.isoformat(),
    )


@app.route("/refresh")
def refresh():
    get_data(force_refresh=True)
    qs = []
    for k in ("from", "to"):
        v = request.args.get(k)
        if v:
            qs.append(f"{k}={urlquote(v)}")
    for v in request.args.getlist("p"):
        qs.append("p=" + urlquote(v))
    suffix = ("?" + "&".join(qs)) if qs else ""
    return redirect(url_for("index") + suffix)


@app.route("/add", methods=["POST"])
def add():
    raw = (request.form.get("url") or "").strip()
    if not raw:
        return redirect(url_for("index"))
    url = normalize_url(raw)
    title = url
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         timeout=20, allow_redirects=True)
        if r.status_code < 400:
            parsed = feedparser.parse(r.content)
            if parsed.feed.get("title"):
                title = parsed.feed["title"]
    except Exception as e:
        print(f"[add] title не получен для {url}: {e}")
    added = append_to_added_xml(url, title)
    print(f"[add] {'добавлено' if added else 'уже было'}: {title} <{url}>")
    get_data(force_refresh=True)
    return redirect(url_for("index"))


@app.route("/log_playback", methods=["POST"])
def log_playback():
    data = request.get_json(silent=True) or {}
    audio = (data.get("audio") or "").strip()
    if not audio:
        return jsonify({"ok": False, "error": "no audio"}), 400
    entry = {
        "audio":            audio,
        "podcast":          data.get("podcast", ""),
        "title":            data.get("title", ""),
        "link":             data.get("link", ""),
        "start_ts":         int(data.get("start_ts") or time.time()),
        "end_ts":           int(data.get("end_ts") or time.time()),
        "listened_seconds": int(data.get("listened_seconds") or 0),
    }
    added = append_playback(entry)
    return jsonify({"ok": True, "added": added})


@app.route("/audio")
def proxy_audio():
    url = request.args.get("url")
    if not url:
        return "missing url", 400
    fwd_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if "Range" in request.headers:
        fwd_headers["Range"] = request.headers["Range"]
    try:
        r = requests.get(url, stream=True, timeout=30,
                         headers=fwd_headers, allow_redirects=True)
    except requests.RequestException as e:
        return f"upstream error: {e}", 502
    if r.status_code >= 400:
        r.close()
        return f"upstream {r.status_code}", r.status_code
    ct = r.headers.get("Content-Type", "")
    if not ct.lower().startswith("audio"):
        ct = "audio/mpeg"
    resp_headers = {
        "Content-Type": ct,
        "Accept-Ranges": r.headers.get("Accept-Ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return Response(stream_with_context(generate()),
                    status=r.status_code, headers=resp_headers)


# ------------------------- запуск ----------------------------
if __name__ == "__main__":
    print("[startup] проверяю кэш…")
    cached = load_cache_stale()
    if cached is None:
        print("[startup] кэша нет — собираю синхронно (это займёт время)")
        _do_refresh()
    elif cached.get("sources_sig") != sources_signature():
        print("[startup] источники изменились — собираю синхронно")
        _do_refresh()
    else:
        print("[startup] кэш актуален")
        if _needs_refresh(cached):
            _start_bg_refresh()

    from waitress import serve
    print(f"Слушаю на http://{HOST}:{PORT} (waitress, threads={THREADS})")
    serve(app, host=HOST, port=PORT, threads=THREADS)
