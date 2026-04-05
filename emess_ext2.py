#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 2 — שידורים חוזרים מאתר אמס (emess.co.il / קול חי)

import os
import json
import time
import logging
import mimetypes
import requests

DATA_DIR = os.environ.get("DATA_DIR", ".")

SHOWS = [
    {"name": "הפוך על הפוך",       "tax_id": 394},
    {"name": "טובים השניים",        "tax_id": 455},
    {"name": "המהדורה המרכזית",     "tax_id": 3634},
    {"name": "בקצב היום",           "tax_id": 448},
    {"name": "מהדורת הבוקר",        "tax_id": 421},
    {"name": "סוגרים שבוע",         "tax_id": 456},
    {"name": "סדר שלישי",           "tax_id": 518},
]

CONFIG = {
    "target_extension": "2",
    "api_base": "https://www.emess.co.il/wp-json/wp/v2/aryo_programs",
    "check_interval_seconds": 300,
    "state_file": os.path.join(DATA_DIR, "state_emess.json"),
    "downloads_dir": os.path.join(DATA_DIR, "emess_downloads"),
    "timeout": 60,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("emess-ext2")


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
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    path = CONFIG["state_file"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_episodes(tax_id: int) -> list:
    params = {
        "tax_broadcasters": tax_id,
        "per_page": 5,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date,title,audio_in_content",
    }
    headers = {
        "accept": "application/json",
        "user-agent": "Mozilla/5.0",
        "referer": "https://www.emess.co.il/",
    }
    r = requests.get(CONFIG["api_base"], params=params, headers=headers, timeout=CONFIG["timeout"])
    r.raise_for_status()
    return r.json()


def get_audio_url(episode: dict) -> str:
    audios = episode.get("audio_in_content", [])
    if audios and audios[0].get("audio_file"):
        return audios[0]["audio_file"]
    return ""


def download_mp3(url: str, path: str):
    headers = {
        "user-agent": "Mozilla/5.0",
        "referer": "https://www.emess.co.il/",
        "accept": "*/*",
    }
    with requests.get(url, headers=headers, timeout=CONFIG["timeout"] * 10, stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)


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
            timeout=CONFIG["timeout"] * 8,
        )
    r.raise_for_status()
    result = r.json()
    if isinstance(result, dict) and result.get("responseStatus") == "EXCEPTION":
        raise RuntimeError(f"שגיאת API: {result}")
    return result


def process_show(show: dict, state: dict):
    tax_id = show["tax_id"]
    name = show["name"]
    key = str(tax_id)
    show_state = state.setdefault(key, {"last_id": 0, "initialized": False})
    last_id = show_state["last_id"]
    initialized = show_state["initialized"]

    try:
        episodes = fetch_episodes(tax_id)
    except Exception as e:
        logger.exception(f"{name}: שגיאה בקריאת API: {e}")
        return

    if not episodes:
        return

    if not initialized:
        max_id = max(ep["id"] for ep in episodes)
        show_state["last_id"] = max_id
        show_state["initialized"] = True
        save_state(state)
        logger.info(f"{name}: אותחל. ID אחרון: {max_id}")
        return

    new_eps = sorted(
        [ep for ep in episodes if ep["id"] > last_id],
        key=lambda ep: ep["id"]
    )

    if not new_eps:
        logger.info(f"{name}: אין פרקים חדשים")
        return

    logger.info(f"{name}: נמצאו {len(new_eps)} פרקים חדשים")

    for ep in new_eps:
        ep_id = ep["id"]
        title = ep.get("title", {}).get("rendered", str(ep_id))
        audio_url = get_audio_url(ep)

        if not audio_url:
            logger.warning(f"{name} #{ep_id}: אין קובץ אודיו — מדלג")
            show_state["last_id"] = ep_id
            save_state(state)
            continue

        ensure_dir(CONFIG["downloads_dir"])
        local_path = os.path.join(CONFIG["downloads_dir"], f"emess_{ep_id}.mp3")

        try:
            logger.info(f"{name} #{ep_id}: מוריד {audio_url}")
            download_mp3(audio_url, local_path)

            result = upload_to_yemot(local_path)
            logger.info(f"{name} #{ep_id}: הועלה | {title} | {result}")

            try:
                os.remove(local_path)
            except Exception:
                pass

        except Exception as e:
            logger.exception(f"{name} #{ep_id}: שגיאה: {e}")
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass
            continue

        show_state["last_id"] = ep_id
        save_state(state)
        time.sleep(2)


def process_once():
    state = load_state()
    for show in SHOWS:
        process_show(show, state)


def main():
    ensure_dir(CONFIG["downloads_dir"])
    get_api_key()
    logger.info("התחיל — שידורים חוזרים אמס שלוחה 2")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    logger.info(f"תוכניות: {', '.join(s['name'] for s in SHOWS)}")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה כללית: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
