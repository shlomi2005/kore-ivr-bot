#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import os
import re
import json
import time
import logging
import mimetypes
import subprocess
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote

import requests
import edge_tts
from hebrew_time import time_to_hebrew


DATA_DIR = os.environ.get("DATA_DIR", ".")

CONFIG = {
    # ימות המשיח
    "target_extension": "303",
    "yemot_private": False,
    "convert_audio": True,

    # האתר למעקב
    "feed_url": "https://newsmusic.blogspot.com/feeds/posts/default?alt=json&max-results=30",

    # כל כמה זמן לבדוק
    "check_interval_seconds": 300,

    # קבצים מקומיים
    "download_dir": os.path.join(DATA_DIR, "downloads_musiclik"),
    "tts_dir": os.path.join(DATA_DIR, "tts_musiclik"),
    "compressed_dir": os.path.join(DATA_DIR, "compressed_musiclik"),
    "state_file": os.path.join(DATA_DIR, "state_musiclik.json"),

    # רשת
    "timeout": 60,
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) MusiclikToYemot/1.0",

    # קריינות — קול גברי עברית
    "tts_voice": "he-IL-AvriNeural",

    # הגבלת גודל העלאה
    "max_upload_size_mb": 45,
    "ffmpeg_bitrate": "48k",
    "ffmpeg_sample_rate": "22050",
    "ffmpeg_channels": "1",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("newsmusic-ext303")


ARCHIVE_MP3_RE = re.compile(
    r'https://[^"\'>\s]*archive\.org[^"\'>\s]+\.mp3',
    re.IGNORECASE
)

HREF_RE = re.compile(
    r'href=["\']([^"\']+)["\']',
    re.IGNORECASE
)


def get_api_key() -> str:
    api_key = os.environ.get("YEMOT_API_KEY", "")
    api_key = api_key.strip().strip("'\"׳״` ")

    if not api_key:
        raise RuntimeError("לא נמצא YEMOT_API_KEY במשתני הסביבה")

    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        raise RuntimeError(f"YEMOT_API_KEY מכיל תווים לא תקינים: {repr(api_key)}")

    return api_key


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "uploaded_audio_urls": [],
            "seen_post_ids": [],
            "initialized": False,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data.setdefault("uploaded_audio_urls", [])
    data.setdefault("seen_post_ids", [])
    data.setdefault("initialized", False)
    return data


def save_state(path: str, state: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def get_yemot_base() -> str:
    if CONFIG["yemot_private"]:
        return "https://private.call2all.co.il/ym/api/"
    return "https://www.call2all.co.il/ym/api/"


def fetch_text(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": CONFIG["user_agent"]},
        timeout=CONFIG["timeout"],
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def fetch_json(url: str):
    response = requests.get(
        url,
        headers={"User-Agent": CONFIG["user_agent"]},
        timeout=CONFIG["timeout"],
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.json()


def parse_iso_datetime(value: str):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def extract_entries_from_feed(feed_json):
    out = []
    feed = feed_json.get("feed", {})
    entries = feed.get("entry", [])

    for entry in entries:
        post_id = None
        title = ""
        published = None
        post_url = None

        if "id" in entry and "$t" in entry["id"]:
            post_id = entry["id"]["$t"]

        if "title" in entry and "$t" in entry["title"]:
            title = entry["title"]["$t"].strip()

        if "published" in entry and "$t" in entry["published"]:
            published = parse_iso_datetime(entry["published"]["$t"])

        for link in entry.get("link", []):
            if link.get("rel") == "alternate" and link.get("type") == "text/html":
                post_url = link.get("href")
                break

        if post_id and post_url:
            out.append({
                "post_id": post_id,
                "title": title or "unknown-title",
                "published": published,
                "post_url": post_url,
            })

    return out


def extract_archive_audio_url(post_html: str):
    m = ARCHIVE_MP3_RE.search(post_html)
    if m:
        return m.group(0)

    hrefs = HREF_RE.findall(post_html)
    for href in hrefs:
        href_lower = href.lower()
        if "archive.org" in href_lower and href_lower.endswith(".mp3"):
            return href

    return None


def remote_exists(url: str) -> bool:
    try:
        r = requests.head(url, timeout=CONFIG["timeout"], allow_redirects=True)
        if r.status_code == 200:
            return True

        r = requests.get(
            url,
            headers={"Range": "bytes=0-0"},
            timeout=CONFIG["timeout"],
            allow_redirects=True,
            stream=True
        )
        return r.status_code in (200, 206)
    except requests.RequestException:
        return False


def sanitize_filename(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    safe = re.sub(r"\s+", " ", safe)
    if not safe:
        safe = f"audio_{int(time.time())}"
    return safe


def normalize_filename_from_url(url: str) -> str:
    raw_name = os.path.basename(url.split("?", 1)[0])
    raw_name = unquote(raw_name)
    safe = sanitize_filename(raw_name)
    if not safe.lower().endswith(".mp3"):
        safe += ".mp3"
    return safe


def get_file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def download_file(url: str, download_dir: str) -> str:
    ensure_dir(download_dir)
    filename = normalize_filename_from_url(url)
    local_path = os.path.join(download_dir, filename)

    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        logger.info(f"הקובץ כבר קיים מקומית: {local_path}")
        return local_path

    with requests.get(url, timeout=CONFIG["timeout"], stream=True) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)

    logger.info(f"הורד: {local_path}")
    return local_path


def israel_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")

def build_tts_text(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    t = time_to_hebrew(israel_time())
    if " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        song = parts[1].strip()
        return f"{t} במוקד המוזיקה הזמר {artist} בשיר {song}"
    else:
        return f"{t} במוקד המוזיקה {title}"


async def _tts_async(text: str, output_path: str):
    last_error = None
    for attempt in range(5):
        try:
            communicate = edge_tts.Communicate(text, CONFIG["tts_voice"])
            await communicate.save(output_path)
            return
        except Exception as e:
            last_error = e
            await asyncio.sleep(3)
    raise last_error


def create_tts_file(title: str, tts_dir: str) -> str:
    ensure_dir(tts_dir)

    tts_text = build_tts_text(title)
    base_name = sanitize_filename(f"tts_{title}")
    output_path = os.path.join(tts_dir, f"{base_name}.mp3")

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        logger.info(f"קובץ הקריינות כבר קיים: {output_path}")
        return output_path

    logger.info(f"יוצר קריינות: {tts_text}")
    asyncio.run(_tts_async(tts_text, output_path))

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("יצירת הקריינות נכשלה: לא נוצר קובץ תקין")

    logger.info(f"נוצר קובץ קריינות: {output_path}")
    return output_path


def compress_audio_if_needed(input_path: str) -> str:
    max_size_mb = CONFIG["max_upload_size_mb"]
    current_size_mb = get_file_size_mb(input_path)

    if current_size_mb <= max_size_mb:
        logger.info(f"הקובץ קטן מספיק להעלאה: {current_size_mb:.2f}MB")
        return input_path

    ensure_dir(CONFIG["compressed_dir"])

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(CONFIG["compressed_dir"], f"{sanitize_filename(base_name)}_compressed.mp3")

    logger.info(f"הקובץ גדול מדי ({current_size_mb:.2f}MB), מבצע דחיסה...")

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ac", CONFIG["ffmpeg_channels"],
        "-ar", CONFIG["ffmpeg_sample_rate"],
        "-b:a", CONFIG["ffmpeg_bitrate"],
        output_path
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg לא מותקן או לא זמין ב-PATH")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"שגיאה בדחיסת אודיו: {e}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("נכשלה יצירת גרסה דחוסה")

    logger.info(f"נוצר קובץ דחוס: {output_path} ({get_file_size_mb(output_path):.2f}MB)")
    return output_path


def guess_mime_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def upload_file_to_yemot(local_path: str):
    api_key = get_api_key()
    upload_url = get_yemot_base() + "UploadFile"
    path_value = f"ivr2:{CONFIG['target_extension'].strip('/')}"

    data = {
        "path": path_value,
        "autoNumbering": "1",
        "convertAudio": "1" if CONFIG["convert_audio"] else "0",
    }

    mime_type = guess_mime_type(local_path)
    logger.info(f"מעלה ליעד: {path_value} | קובץ: {os.path.basename(local_path)}")

    with open(local_path, "rb") as f:
        response = requests.post(
            upload_url,
            data=data,
            files={"file": (os.path.basename(local_path), f, mime_type)},
            headers={"authorization": api_key},
            timeout=CONFIG["timeout"] * 2
        )

    if response.status_code != 200:
        raise RuntimeError(f"שגיאת HTTP בהעלאה: {response.status_code} | {response.text}")

    try:
        result = response.json()
    except Exception:
        result = {"raw": response.text}

    if isinstance(result, dict) and result.get("responseStatus") == "EXCEPTION":
        raise RuntimeError(f"שגיאת API בהעלאה: {result}")

    return result


def process_once():
    state = load_state(CONFIG["state_file"])
    uploaded_audio_urls = set(state.get("uploaded_audio_urls", []))
    seen_post_ids = set(state.get("seen_post_ids", []))
    initialized = bool(state.get("initialized", False))

    logger.info(f"סורק את הפיד: {CONFIG['feed_url']}")
    feed_json = fetch_json(CONFIG["feed_url"])
    entries = extract_entries_from_feed(feed_json)
    logger.info(f"נמצאו {len(entries)} פוסטים בפיד")

    entries = sorted(
        entries,
        key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc)
    )

    if not initialized:
        logger.info("ריצה ראשונה: מסמן את הפוסטים הקיימים ככבר נראו, מלבד האחרון")

        for entry in entries[:-10]:
            seen_post_ids.add(entry["post_id"])

        state["seen_post_ids"] = sorted(seen_post_ids)
        state["initialized"] = True
        save_state(CONFIG["state_file"], state)

        logger.info("הסקריפט אותחל. מעלה את 10 הפוסטים האחרונים...")

    found_any_new = False

    for entry in entries:
        post_id = entry["post_id"]
        post_url = entry["post_url"]
        title = entry["title"]

        if post_id in seen_post_ids:
            continue

        logger.info(f"בודק פוסט חדש: {title}")

        try:
            post_html = fetch_text(post_url)
        except Exception as e:
            logger.exception(f"שגיאה בקריאת פוסט {post_url}: {e}")
            continue

        audio_url = extract_archive_audio_url(post_html)

        if not audio_url:
            logger.info("לא נמצא קישור archive.org mp3 בפוסט")
            seen_post_ids.add(post_id)
            state["seen_post_ids"] = sorted(seen_post_ids)
            save_state(CONFIG["state_file"], state)
            continue

        logger.info(f"נמצא אודיו: {audio_url}")

        if audio_url in uploaded_audio_urls:
            logger.info("האודיו כבר הועלה בעבר")
            seen_post_ids.add(post_id)
            state["seen_post_ids"] = sorted(seen_post_ids)
            save_state(CONFIG["state_file"], state)
            continue

        if not remote_exists(audio_url):
            logger.info("קישור האודיו לא זמין כרגע")
            continue

        song_path = download_file(audio_url, CONFIG["download_dir"])
        song_path_for_upload = compress_audio_if_needed(song_path)
        tts_path = create_tts_file(title, CONFIG["tts_dir"])

        song_result = upload_file_to_yemot(song_path_for_upload)
        logger.info(f"השיר הועלה: {song_result}")

        tts_result = upload_file_to_yemot(tts_path)
        logger.info(f"הקריינות הועלתה: {tts_result}")

        # מחיקת קבצים מקומיים לחיסכון במקום
        for path in set([song_path, song_path_for_upload, tts_path]):
            try:
                os.remove(path)
                logger.info(f"נמחק: {path}")
            except Exception:
                pass

        found_any_new = True

        uploaded_audio_urls.add(audio_url)
        seen_post_ids.add(post_id)
        state["uploaded_audio_urls"] = sorted(uploaded_audio_urls)
        state["seen_post_ids"] = sorted(seen_post_ids)
        save_state(CONFIG["state_file"], state)

    if not found_any_new:
        logger.info("לא נמצא שיר חדש להעלאה בסבב הזה")


def main():
    ensure_dir(CONFIG["download_dir"])
    ensure_dir(CONFIG["tts_dir"])
    ensure_dir(CONFIG["compressed_dir"])
    get_api_key()

    logger.info("הסקריפט התחיל — שירים חדשים שלוחה 303")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")

    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")

        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
