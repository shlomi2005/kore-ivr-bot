#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 304 — מבזקים (kore.co.il/mplus)

import asyncio
import os
import re
import json
import time
import logging
import mimetypes

import requests
import edge_tts

DATA_DIR = os.environ.get("DATA_DIR", ".")

CONFIG = {
    "target_extension": "304",
    "yemot_private": False,
    "convert_audio": True,
    "api_url": "https://www.kore.co.il/api_mplus/mplus/results",
    "check_interval_seconds": 120,
    "tts_dir": os.path.join(DATA_DIR, "tts_kore"),
    "state_file": os.path.join(DATA_DIR, "state_kore.json"),
    "timeout": 30,
    "tts_voice": "he-IL-AvriNeural",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("kore-mplus-ext304")


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
        return {"seen_ids": [], "initialized": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen_ids": [], "initialized": False}


def save_state(state: dict):
    path = CONFIG["state_file"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_items() -> list:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json; charset=UTF-8",
        "origin": "https://www.kore.co.il",
        "referer": "https://www.kore.co.il/mplus",
        "user-agent": "Mozilla/5.0",
    }
    r = requests.post(CONFIG["api_url"], json={"page": 0}, headers=headers, timeout=CONFIG["timeout"])
    r.raise_for_status()
    return r.json().get("data", {}).get("items", [])


def clean_text(text: str) -> str:
    text = re.sub(r"\r\n|\r|\n", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def build_tts_text(item: dict) -> str:
    content = clean_text(item.get("short_text", ""))
    return f"מבזק פלוס. {content}"


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


def create_tts_file(item_id: int, text: str) -> str:
    ensure_dir(CONFIG["tts_dir"])
    path = os.path.join(CONFIG["tts_dir"], f"mplus_{item_id}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    logger.info(f"יוצר TTS: {text[:60]}")
    asyncio.run(_tts_async(text, path))
    return path


def upload_to_yemot(local_path: str):
    api_key = get_api_key()
    url = "https://www.call2all.co.il/ym/api/UploadFile"
    path_value = f"ivr2:{CONFIG['target_extension']}"
    mime, _ = mimetypes.guess_type(local_path)
    with open(local_path, "rb") as f:
        r = requests.post(
            url,
            data={"path": path_value, "autoNumbering": "1", "convertAudio": "1"},
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
    seen_ids = set(state.get("seen_ids", []))
    initialized = state.get("initialized", False)

    logger.info("סורק kore.co.il/mplus")
    items = fetch_items()
    logger.info(f"התקבלו {len(items)} פריטים")

    if not items:
        return

    if not initialized:
        # ריצה ראשונה — סמן הכל כנראה
        for item in items:
            seen_ids.add(item["id"])
        state["seen_ids"] = sorted(seen_ids)
        state["initialized"] = True
        save_state(state)
        logger.info(f"אותחל. {len(seen_ids)} פריטים סומנו")
        return

    # חדשים בלבד, מהישן לחדש
    new_items = sorted(
        [i for i in items if i["id"] not in seen_ids],
        key=lambda x: x["id"]
    )

    if not new_items:
        logger.info("אין מבזקים חדשים")
        return

    logger.info(f"נמצאו {len(new_items)} מבזקים חדשים")

    for item in new_items:
        content = item.get("short_text", "").strip()
        if not content:
            seen_ids.add(item["id"])
            continue

        item_id = item["id"]
        tts_text = build_tts_text(item)

        try:
            tts_path = create_tts_file(item_id, tts_text)
            result = upload_to_yemot(tts_path)
            logger.info(f"הועלה #{item_id}: {tts_text[:60]} | {result}")
            try:
                os.remove(tts_path)
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"שגיאה בפריט #{item_id}: {e}")
            continue

        seen_ids.add(item_id)
        state["seen_ids"] = sorted(seen_ids)
        save_state(state)
        time.sleep(1)


def main():
    ensure_dir(CONFIG["tts_dir"])
    get_api_key()
    logger.info("התחיל — kore mplus שלוחה 304")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
