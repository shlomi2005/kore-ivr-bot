#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 2 — שידורים חוזרים מאתר אמס (emess.co.il / קול חי)

import asyncio
import os
import json
import time
import logging
import mimetypes
from datetime import datetime

import requests
import edge_tts

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

DAYS_HE = {
    6: "יום ראשון",
    0: "יום שני",
    1: "יום שלישי",
    2: "יום רביעי",
    3: "יום חמישי",
    4: "יום שישי",
    5: "שבת",
}

CONFIG = {
    "target_extension": "2",
    "api_base": "https://www.emess.co.il/wp-json/wp/v2/aryo_programs",
    "check_interval_seconds": 300,
    "state_file": os.path.join(DATA_DIR, "state_emess.json"),
    "downloads_dir": os.path.join(DATA_DIR, "emess_downloads"),
    "tts_voice": "he-IL-AvriNeural",
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


def episode_day_he(episode: dict) -> str:
    """מחזיר שם היום בעברית לפי תאריך הפרק"""
    try:
        dt = datetime.fromisoformat(episode["date"])
        return DAYS_HE.get(dt.weekday(), "")
    except Exception:
        return ""


async def _tts_async(text: str, path: str):
    last_err = None
    for _ in range(5):
        try:
            await edge_tts.Communicate(text, CONFIG["tts_voice"]).save(path)
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(3)
    raise last_err


def create_tts_announcement(ep_id: int, show_name: str, day_he: str) -> str:
    ensure_dir(CONFIG["downloads_dir"])
    path = os.path.join(CONFIG["downloads_dir"], f"announce_{ep_id}.mp3")
    text = f"{day_he} במוקד הפודקאסט {show_name}"
    logger.info(f"יוצר הודעה: {text}")
    asyncio.run(_tts_async(text, path))
    return path


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


def upload_episode(ep: dict, show_name: str):
    """מעלה הודעת הכרזה ואז את קובץ השידור"""
    ep_id = ep["id"]
    audio_url = get_audio_url(ep)
    if not audio_url:
        logger.warning(f"{show_name} #{ep_id}: אין קובץ אודיו — מדלג")
        return False

    day_he = episode_day_he(ep)
    title = ep.get("title", {}).get("rendered", str(ep_id))
    ensure_dir(CONFIG["downloads_dir"])

    announce_path = os.path.join(CONFIG["downloads_dir"], f"announce_{ep_id}.mp3")
    episode_path = os.path.join(CONFIG["downloads_dir"], f"emess_{ep_id}.mp3")

    try:
        # 1. קובץ השידור (עולה ראשון = מושמע שני)
        logger.info(f"{show_name} #{ep_id}: מוריד {audio_url}")
        download_mp3(audio_url, episode_path)
        upload_to_yemot(episode_path)
        logger.info(f"{show_name} #{ep_id}: שידור הועלה | {title}")
        try:
            os.remove(episode_path)
        except Exception:
            pass

        # 2. הודעת הכרזה (עולה שני = מושמעת ראשונה)
        announce_path = create_tts_announcement(ep_id, show_name, day_he)
        upload_to_yemot(announce_path)
        logger.info(f"{show_name} #{ep_id}: הכרזה הועלתה")
        try:
            os.remove(announce_path)
        except Exception:
            pass

        return True

    except Exception as e:
        logger.exception(f"{show_name} #{ep_id}: שגיאה: {e}")
        for p in [announce_path, episode_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return False


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
        # העלה את הפרק האחרון בלבד
        latest = max(episodes, key=lambda ep: ep["id"])
        logger.info(f"{name}: אתחול — מעלה פרק אחרון #{latest['id']}")
        if upload_episode(latest, name):
            show_state["last_id"] = latest["id"]
        else:
            show_state["last_id"] = latest["id"]  # דלג גם אם נכשל
        show_state["initialized"] = True
        save_state(state)
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
        upload_episode(ep, name)
        show_state["last_id"] = ep["id"]
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
