#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 301 — עדכוני פוליטיקה (authenti.newsupdates.click)

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
    "target_extension": "301",
    "yemot_private": False,
    "convert_audio": True,
    "api_url": "https://authenti.newsupdates.click/api/get_messages_optimized.php",
    "api_source": "עדכוני פוליטיקה",
    "check_interval_seconds": 120,
    "tts_dir": os.path.join(DATA_DIR, "tts_politics"),
    "state_file": os.path.join(DATA_DIR, "state_politics.json"),
    "timeout": 30,
    "tts_voice": "he-IL-AvriNeural",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("politics-ext301")


def get_api_key() -> str:
    key = os.environ.get("YEMOT_API_KEY", "").strip().strip("'\"׳״` ")
    if not key:
        raise RuntimeError("לא נמצא YEMOT_API_KEY")
    key.encode("latin-1")
    return key


def get_cookies() -> dict:
    return {
        "PHPSESSID": os.environ.get("NEWSUPDATES_PHPSESSID", "kvh2n8p4jm7ko44d62qo378mkp"),
        "user_sess": os.environ.get("NEWSUPDATES_USER_SESS", "7de4873c2bca5cda7b859a0d0daad87017124f227ae9b40e97b3618c307b1133"),
    }


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


def fetch_messages(last_id: int) -> list:
    params = {"source": CONFIG["api_source"], "last_id": last_id, "limit": 50}
    headers = {"accept": "*/*", "referer": "https://authenti.newsupdates.click/", "user-agent": "Mozilla/5.0"}
    r = requests.get(CONFIG["api_url"], params=params, headers=headers, cookies=get_cookies(), timeout=CONFIG["timeout"])
    r.raise_for_status()
    return r.json().get("messages", [])


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def build_tts_text(msg: dict) -> str:
    t = msg.get("formatted_time", "").strip()
    desc = clean_text(msg.get("description", ""))
    prefix = f"{t} " if t else ""
    return f"{prefix}במוקד הפוליטי {desc}"


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


def create_tts_file(msg_id: int, text: str) -> str:
    ensure_dir(CONFIG["tts_dir"])
    path = os.path.join(CONFIG["tts_dir"], f"msg_{msg_id}.mp3")
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
    last_id = state.get("last_id", 0)
    initialized = state.get("initialized", False)

    logger.info(f"בודק עדכוני פוליטיקה מ-ID: {last_id}")
    messages = fetch_messages(last_id)
    logger.info(f"התקבלו {len(messages)} הודעות")

    if not messages:
        return

    if not initialized:
        max_id = max(m["id"] for m in messages)
        state["last_id"] = max_id
        state["initialized"] = True
        save_state(state)
        logger.info(f"אותחל. ID אחרון: {max_id}")
        return

    for msg in sorted(messages, key=lambda m: m["id"]):
        if msg.get("admin_only"):
            continue
        desc = msg.get("description", "").strip()
        if not desc:
            continue

        msg_id = msg["id"]
        tts_text = build_tts_text(msg)

        try:
            tts_path = create_tts_file(msg_id, tts_text)
            result = upload_to_yemot(tts_path)
            logger.info(f"הועלה #{msg_id}: {tts_text[:60]} | {result}")
            try:
                os.remove(tts_path)
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"שגיאה #{msg_id}: {e}")
            continue

        state["last_id"] = msg_id
        save_state(state)
        time.sleep(1)


def main():
    ensure_dir(CONFIG["tts_dir"])
    get_api_key()
    logger.info("התחיל — עדכוני פוליטיקה שלוחה 301")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
