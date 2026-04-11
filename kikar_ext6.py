#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 6 — חסידות (kikar.co.il/hasidism)

import asyncio
import os
import re
import json
import time
import logging
import mimetypes
from datetime import datetime, timezone, timedelta

import requests
import edge_tts
from curl_cffi import requests as cffi_requests
from hebrew_time import time_to_hebrew

DATA_DIR = os.environ.get("DATA_DIR", ".")

CONFIG = {
    "target_extension": "6",
    "page_url": "https://www.kikar.co.il/hasidism",
    "check_interval_seconds": 180,
    "tts_dir": os.path.join(DATA_DIR, "tts_kikar"),
    "state_file": os.path.join(DATA_DIR, "state_kikar.json"),
    "timeout": 30,
    "tts_voice": "he-IL-AvriNeural",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("kikar-ext6")


def get_api_key() -> str:
    key = os.environ.get("YEMOT_API_KEY", "").strip().strip("'\"׳״` ")
    if not key:
        raise RuntimeError("לא נמצא YEMOT_API_KEY")
    key.encode("latin-1")
    return key


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def load_state() -> dict:
    path = CONFIG["state_file"]
    if not os.path.exists(path):
        return {"last_id": 0, "initialized": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_id": 0, "initialized": False}


def save_state(state: dict):
    path = CONFIG["state_file"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_articles() -> list:
    """מביא כתבות מ-kikar.co.il דרך RSC endpoint"""
    headers = {
        "RSC": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Referer": CONFIG["page_url"],
    }
    r = cffi_requests.get(
        CONFIG["page_url"],
        headers=headers,
        impersonate="chrome110",
        timeout=CONFIG["timeout"],
    )
    r.raise_for_status()
    data = r.text

    # חילוץ כתבות מתוך ה-RSC stream
    articles = []
    # דפוס: {"id":1234567,"slug":"...","author":"...","categorySlug":"hasidism",...,"title":"...","subTitle":"...",...,"time":...}
    pattern = re.compile(
        r'"id":(\d{6,8})'
        r'[^}]{0,200}?"categorySlug":"hasidism"'
        r'[^}]{0,500}?"title":"([^"\\]{5,200})"'
        r'(?:[^}]{0,1000}?"subTitle":"([^"\\]{0,300})")?'
        r'[^}]{0,300}?"time":(\d{13})',
        re.DOTALL,
    )
    seen_ids = set()
    for m in pattern.finditer(data):
        article_id = int(m.group(1))
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)
        articles.append({
            "id": article_id,
            "title": _unescape(m.group(2)),
            "subTitle": _unescape(m.group(3) or ""),
            "time": int(m.group(4)),
        })

    articles.sort(key=lambda a: a["id"])
    return articles


def _unescape(s: str) -> str:
    """מנקה escape sequences בסיסיות"""
    return s.replace('\\"', '"').replace('\\n', ' ').replace('\\t', ' ').strip()


def israel_day() -> str:
    days = ["יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "שבת קודש", "יום ראשון"]
    now = datetime.now(timezone.utc) + timedelta(hours=3)
    return days[now.weekday()]


def build_tts_text(article: dict) -> str:
    day = israel_day()
    title = article.get("title", "").strip()
    subtitle = article.get("subTitle", "").strip()

    text = f"{day} בכיכר השבת: {title}"
    if subtitle:
        # מגביל סאבטייטל ל-200 תווים כדי לא לעמוס
        text += f". {subtitle[:200]}"
    return text


async def _tts_async(text: str, output_path: str):
    last_error = None
    for _ in range(5):
        try:
            await edge_tts.Communicate(text, CONFIG["tts_voice"]).save(output_path)
            return
        except Exception as e:
            last_error = e
            await asyncio.sleep(3)
    raise last_error


def create_tts_file(article_id: int, text: str) -> str:
    ensure_dir(CONFIG["tts_dir"])
    path = os.path.join(CONFIG["tts_dir"], f"kikar_{article_id}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    logger.info(f"יוצר TTS: {text[:70]}")
    asyncio.run(_tts_async(text, path))
    return path


def upload_to_yemot(local_path: str):
    api_key = get_api_key()
    mime, _ = mimetypes.guess_type(local_path)
    with open(local_path, "rb") as f:
        r = requests.post(
            "https://www.call2all.co.il/ym/api/UploadFile",
            data={"path": f"ivr2:{CONFIG['target_extension']}", "autoNumbering": "1", "convertAudio": "1"},
            files={"file": (os.path.basename(local_path), f, mime or "audio/mpeg")},
            headers={"authorization": api_key},
            timeout=CONFIG["timeout"] * 3,
        )
    r.raise_for_status()
    result = r.json()
    if isinstance(result, dict) and result.get("responseStatus") == "EXCEPTION":
        raise RuntimeError(f"שגיאת API: {result}")
    return result


def process_once():
    state = load_state()
    last_id = state.get("last_id", 0)
    initialized = state.get("initialized", False)

    logger.info(f"סורק kikar.co.il/hasidism מ-ID: {last_id}")
    try:
        articles = fetch_articles()
    except Exception as e:
        logger.exception(f"שגיאה בסריקה: {e}")
        return

    if not articles:
        logger.warning("לא נמצאו כתבות")
        return

    if not initialized:
        backfill = articles[-3:]
        state["last_id"] = backfill[0]["id"] - 1
        last_id = state["last_id"]
        state["initialized"] = True
        save_state(state)
        logger.info(f"אותחל. מעבד {len(backfill)} כתבות אחרונות")

    new_articles = [a for a in articles if a["id"] > last_id]

    if not new_articles:
        logger.info("אין כתבות חדשות")
        return

    logger.info(f"נמצאו {len(new_articles)} כתבות חדשות")

    for article in new_articles:
        article_id = article["id"]
        tts_text = build_tts_text(article)
        tts_path = None

        try:
            tts_path = create_tts_file(article_id, tts_text)
            result = upload_to_yemot(tts_path)
            logger.info(f"הועלה #{article_id}: {tts_text[:70]} | {result}")
        except Exception as e:
            logger.exception(f"שגיאה #{article_id}: {e}")
            continue
        finally:
            if tts_path and os.path.exists(tts_path):
                try:
                    os.remove(tts_path)
                except Exception:
                    pass

        state["last_id"] = article_id
        save_state(state)
        time.sleep(1)


def main():
    ensure_dir(CONFIG["tts_dir"])
    get_api_key()
    logger.info("התחיל — חסידות כיכר השבת שלוחה 6")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
