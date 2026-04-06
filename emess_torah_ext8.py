#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 8 — שידורים תורניים חוזרים מאתר אמס (emess.co.il / קול חי)

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
    # לימוד יומי
    {"name": "הדף היומי - הרב בנימין מילצקי",          "tax_id": 3714},
    {"name": "הדף היומי - ירושלמי",                     "tax_id": 406},
    {"name": "הדף היומי",                               "tax_id": 425},
    {"name": "הרמב\"ם היומי",                           "tax_id": 574},
    {"name": "המשנה היומית",                            "tax_id": 47439},
    # פרשת השבוע
    {"name": "הפרשה - הרב מרדכי מלכה",                 "tax_id": 3739},
    {"name": "הפרשה עם הרב אביחי קצין",                "tax_id": 374},
    {"name": "לחיות את הפרשה",                         "tax_id": 387},
    {"name": "השבוע בפרשה - הרב שמעון אליטוב",         "tax_id": 554},
    {"name": "הפרשה",                                   "tax_id": 405},
    # הלכה
    {"name": "הלכה למעשה",                             "tax_id": 517},
    {"name": "הלכה למעשה - הרב אופיר מלכא",            "tax_id": 7658},
    {"name": "הליכות ביתה",                             "tax_id": 593},
    {"name": "חידון הלכה למעשה העולמי",                 "tax_id": 528},
    {"name": "כשרות הממון",                             "tax_id": 33936},
    # השראה ומוסר
    {"name": "התעוררות",                                "tax_id": 437},
    {"name": "התעוררות - ידידיה מאיר",                  "tax_id": 6802},
    {"name": "השראה יומית",                             "tax_id": 527},
    {"name": "השראה יומית - הרב יצחק פנגר",            "tax_id": 6582},
    {"name": "אורחות חיים",                             "tax_id": 435},
    {"name": "כאייל תערוג",                             "tax_id": 396},
    {"name": "כאייל תערוג - הרב אייל אונגר",           "tax_id": 6526},
    {"name": "חישוב מסלול מחדש",                        "tax_id": 506},
    {"name": "דרך חיים",                                "tax_id": 378},
    {"name": "דרך חיים - הרב חיים איידלס",             "tax_id": 5422},
    # רבנים
    {"name": "האור שבחיים - הרב ראובן אלבז",           "tax_id": 525},
    {"name": "היד החזקה - הרב יחיאל ניישטט",           "tax_id": 575},
    {"name": "היד החזקה - הרב מנחם הכט",               "tax_id": 579},
    {"name": "דיבור יהודי - הרב נחמיה רוטנברג",        "tax_id": 591},
    {"name": "אור התורה",                               "tax_id": 498},
    # אמונה ומחשבה
    {"name": "הנפש בראי היהדות",                        "tax_id": 449},
    {"name": "ואמונתך בלילות",                          "tax_id": 608},
    {"name": "יש אמונה",                                "tax_id": 476},
    {"name": "ידיד נפש",                                "tax_id": 463},
    {"name": "יהדות מזווית שונה",                       "tax_id": 372},
    {"name": "יחל ישראל",                               "tax_id": 414},
    {"name": "בין קודש לחול",                           "tax_id": 590},
    {"name": "בית נאמן",                                "tax_id": 612},
    {"name": "במבט יהודי",                              "tax_id": 3982},
    # ישיבה ולימוד
    {"name": "השטייגניסט",                              "tax_id": 384},
    {"name": "בר בי רב - יום שכולו תורה",              "tax_id": 3931},
    {"name": "השיעור השבועי",                           "tax_id": 3690},
    {"name": "בין הזמנים",                              "tax_id": 644},
    # התוועדויות וחסידות
    {"name": "התוועדות",                                "tax_id": 423},
    {"name": "חדשות אנ\"ש",                             "tax_id": 637},
    {"name": "הילולא דצדיקיא",                          "tax_id": 3685},
    # חגים ומועדים
    {"name": "חגים ומועדים",                            "tax_id": 390},
    {"name": "חג הפסח",                                 "tax_id": 596},
    {"name": "חג הסוכות",                               "tax_id": 643},
    {"name": "ל\"ג בעומר",                              "tax_id": 409},
    {"name": "חג השבועות",                              "tax_id": 4315},
    {"name": "ימי בין המצרים",                          "tax_id": 3841},
    # שבת
    {"name": "אורח לשבת",                              "tax_id": 3803},
    {"name": "ליל שישי חי",                             "tax_id": 373},
    {"name": "ליל שישי חינוכי",                         "tax_id": 444},
    {"name": "לכבוד שבת קודש",                          "tax_id": 55962},
    {"name": "בהילוך שישי",                             "tax_id": 3973},
    {"name": "הכנה לשבת קודש",                         "tax_id": 57627},
    # חינוך
    {"name": "חינוך עם ערכים",                          "tax_id": 54740},
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
    "target_extension": "8",
    "api_base": "https://www.emess.co.il/wp-json/wp/v2/aryo_programs",
    "check_interval_seconds": 300,
    "state_file": os.path.join(DATA_DIR, "state_torah.json"),
    "downloads_dir": os.path.join(DATA_DIR, "torah_downloads"),
    "tts_voice": "he-IL-AvriNeural",
    "timeout": 60,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("torah-ext8")


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
    if not audios:
        return ""
    first = audios[0]
    if isinstance(first, dict):
        return first.get("audio_file", "")
    if isinstance(first, str) and first.startswith("http"):
        return first
    return ""


def episode_day_he(episode: dict) -> str:
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
    text = f"{day_he} {show_name}"
    logger.info(f"יוצר הכרזה: {text}")
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
    mime, _ = mimetypes.guess_type(local_path)
    with open(local_path, "rb") as f:
        r = requests.post(
            "https://www.call2all.co.il/ym/api/UploadFile",
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


def cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def upload_episode(ep: dict, show_name: str) -> bool:
    ep_id = ep["id"]
    audio_url = get_audio_url(ep)
    if not audio_url:
        logger.info(f"{show_name} #{ep_id}: אין אודיו — מדלג")
        return True  # לא כישלון, פשוט אין אודיו

    day_he = episode_day_he(ep)
    title = ep.get("title", {}).get("rendered", str(ep_id))
    ensure_dir(CONFIG["downloads_dir"])

    episode_path = os.path.join(CONFIG["downloads_dir"], f"torah_{ep_id}.mp3")
    announce_path = os.path.join(CONFIG["downloads_dir"], f"announce_{ep_id}.mp3")

    try:
        # 1. הורד שידור
        logger.info(f"{show_name} #{ep_id}: מוריד {audio_url}")
        download_mp3(audio_url, episode_path)

        # 2. העלה שידור קודם (ימות משמיע שני)
        upload_to_yemot(episode_path)
        logger.info(f"{show_name} #{ep_id}: שידור הועלה | {title}")
        cleanup(episode_path)

        # 3. צור והעלה הכרזה (ימות משמיע ראשון)
        announce_path = create_tts_announcement(ep_id, show_name, day_he)
        upload_to_yemot(announce_path)
        logger.info(f"{show_name} #{ep_id}: הכרזה הועלתה")
        cleanup(announce_path)

        return True

    except Exception as e:
        logger.exception(f"{show_name} #{ep_id}: שגיאה: {e}")
        cleanup(episode_path, announce_path)
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
        logger.warning(f"{name}: שגיאה בקריאת API: {e}")
        return

    if not episodes:
        return

    if not initialized:
        latest = max(episodes, key=lambda ep: ep["id"])
        logger.info(f"{name}: אתחול — מעלה פרק אחרון #{latest['id']}")
        upload_episode(latest, name)
        show_state["last_id"] = latest["id"]
        show_state["initialized"] = True
        save_state(state)
        return

    new_eps = sorted(
        [ep for ep in episodes if ep["id"] > last_id],
        key=lambda ep: ep["id"]
    )

    if not new_eps:
        return

    logger.info(f"{name}: {len(new_eps)} פרקים חדשים")
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
    logger.info(f"התחיל — שידורים תורניים שלוחה 8 | {len(SHOWS)} תוכניות")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה כללית: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
