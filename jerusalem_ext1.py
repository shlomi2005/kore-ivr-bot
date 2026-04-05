#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 1 — חדשות ירושלים (haredim-jerusalem.co.il)

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
from hebrew_time import time_to_hebrew

DATA_DIR = os.environ.get("DATA_DIR", ".")

CONFIG = {
    "target_extension": "1",
    "api_url": "https://haredim-jerusalem.co.il/wp-json/wp/v2/posts",
    "category_id": 1,
    "check_interval_seconds": 120,
    "tts_dir": os.path.join(DATA_DIR, "tts_jerusalem"),
    "state_file": os.path.join(DATA_DIR, "state_jerusalem.json"),
    "timeout": 30,
    "tts_voice": "he-IL-AvriNeural",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("jerusalem-ext1")


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


def fetch_posts(last_id: int) -> list:
    params = {
        "per_page": 20,
        "categories": CONFIG["category_id"],
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date,title,excerpt",
    }
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0"}
    r = requests.get(CONFIG["api_url"], params=params, headers=headers, timeout=CONFIG["timeout"])
    r.raise_for_status()
    return r.json()


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def parse_date_il(date_str: str) -> str:
    """מחזיר שעת ישראל HH:MM מ-WordPress date (שעון מקומי)"""
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def build_tts_text(post: dict) -> str:
    t = time_to_hebrew(parse_date_il(post.get("date", "")))
    title = clean_html(post.get("title", {}).get("rendered", ""))
    excerpt = clean_html(post.get("excerpt", {}).get("rendered", ""))
    prefix = f"{t} " if t else ""
    content = title
    if excerpt and excerpt != title:
        content = f"{title}. {excerpt}"
    return f"{prefix}בחדשות ירושלים {content}"


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


def create_tts_file(post_id: int, text: str) -> str:
    ensure_dir(CONFIG["tts_dir"])
    path = os.path.join(CONFIG["tts_dir"], f"post_{post_id}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    logger.info(f"יוצר TTS: {text[:60]}")
    asyncio.run(_tts_async(text, path))
    return path


def upload_to_yemot(local_path: str):
    api_key = get_api_key()
    url = "https://www.call2all.co.il/ym/api/UploadFile"
    mime, _ = mimetypes.guess_type(local_path)
    with open(local_path, "rb") as f:
        r = requests.post(
            url,
            data={"path": f"ivr2:{CONFIG['target_extension']}", "autoNumbering": "1", "convertAudio": "1"},
            files={"file": (os.path.basename(local_path), f, mime or "audio/mpeg")},
            headers={"authorization": api_key},
            timeout=CONFIG["timeout"] * 2,
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

    logger.info(f"בודק חדשות ירושלים מ-ID: {last_id}")
    try:
        posts = fetch_posts(last_id)
    except Exception as e:
        logger.exception(f"שגיאה בקריאת API: {e}")
        return

    if not posts:
        return

    if not initialized:
        max_id = max(p["id"] for p in posts)
        state["last_id"] = max_id
        state["initialized"] = True
        save_state(state)
        logger.info(f"אותחל. ID אחרון: {max_id}")
        return

    new_posts = sorted([p for p in posts if p["id"] > last_id], key=lambda p: p["id"])

    if not new_posts:
        logger.info("אין חדשות חדשות")
        return

    logger.info(f"נמצאו {len(new_posts)} כתבות חדשות")

    for post in new_posts:
        post_id = post["id"]
        tts_text = build_tts_text(post)

        try:
            tts_path = create_tts_file(post_id, tts_text)
            result = upload_to_yemot(tts_path)
            logger.info(f"הועלה #{post_id}: {tts_text[:60]} | {result}")
            try:
                os.remove(tts_path)
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"שגיאה #{post_id}: {e}")
            continue

        state["last_id"] = post_id
        save_state(state)
        time.sleep(1)


def main():
    ensure_dir(CONFIG["tts_dir"])
    get_api_key()
    logger.info("התחיל — חדשות ירושלים שלוחה 1")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
