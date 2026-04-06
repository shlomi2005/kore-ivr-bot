#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 303 — שירים חדשים (hamenagen.net)

import asyncio
import os
import re
import json
import time
import logging
import mimetypes
import subprocess
from datetime import datetime, timezone, timedelta

import requests
import edge_tts
from curl_cffi import requests as cffi_requests
from hebrew_time import time_to_hebrew

DATA_DIR = os.environ.get("DATA_DIR", ".")

CONFIG = {
    "target_extension": "303",
    "api_url": "https://hamenagen.net/wp-json/wp/v2/posts",
    "category_id": 4,
    "check_interval_seconds": 300,
    "tts_dir": os.path.join(DATA_DIR, "tts_music"),
    "download_dir": os.path.join(DATA_DIR, "dl_music"),
    "state_file": os.path.join(DATA_DIR, "state_music.json"),
    "timeout": 60,
    "tts_voice": "he-IL-AvriNeural",
    "max_upload_size_mb": 45,
    "ffmpeg_bitrate": "48k",
    "ffmpeg_sample_rate": "22050",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("music-ext303")


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


def fetch_posts() -> list:
    params = {
        "per_page": 10,
        "categories": CONFIG["category_id"],
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date,title,video_to_post",
    }
    headers = {
        "accept": "application/json",
        "referer": "https://hamenagen.net/",
    }
    r = cffi_requests.get(
        CONFIG["api_url"], params=params, headers=headers,
        impersonate="chrome110", timeout=CONFIG["timeout"]
    )
    r.raise_for_status()
    return r.json()


def israel_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")


def clean_title(title: str) -> str:
    # הסרת HTML entities
    title = re.sub(r"&[a-zA-Z]+;", " ", title)
    title = re.sub(r"&#\d+;", " ", title)
    # הסרת תווים שנקראים בקול על ידי TTS (אבל לא מקף שמשמש כמפריד אמן/שיר)
    title = re.sub(r"[|•·\[\](){}<>_/\\@#%^*+=~`]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def build_tts_text(title: str) -> str:
    title = clean_title(title)
    t = time_to_hebrew(israel_time())
    if " - " in title:
        parts = title.split(" - ", 1)
        song = parts[0].strip()
        artist = parts[1].strip()
        return f"{t} במוקד המוזיקה הזמר {artist} בשיר {song}"
    return f"{t} במוקד המוזיקה {title}"


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


def create_tts_file(post_id: int, text: str) -> str:
    ensure_dir(CONFIG["tts_dir"])
    path = os.path.join(CONFIG["tts_dir"], f"tts_{post_id}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    logger.info(f"יוצר TTS: {text[:70]}")
    asyncio.run(_tts_async(text, path))
    return path


def download_youtube_audio(youtube_url: str, post_id: int) -> str:
    ensure_dir(CONFIG["download_dir"])
    out_path = os.path.join(CONFIG["download_dir"], f"song_{post_id}.mp3")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    logger.info(f"מוריד מ-YouTube: {youtube_url}")

    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE", "")
    cookies_env = os.environ.get("YOUTUBE_COOKIES", "")

    # כתיבת קובץ עוגיות אם הועבר כ-env var
    tmp_cookies = None
    if cookies_env and not cookies_file:
        tmp_cookies = os.path.join(DATA_DIR, "yt_cookies.txt")
        with open(tmp_cookies, "w") as f:
            f.write(cookies_env)
        cookies_file = tmp_cookies

    cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "128K",
        "--no-playlist",
        "--output", out_path,
        "--no-progress",
        "--extractor-args", "youtube:player_client=android,web",
        "--sleep-interval", "2",
        "--max-sleep-interval", "5",
    ]
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]

    cmd.append(youtube_url)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"yt-dlp לא יצר קובץ: {out_path}")
    return out_path


def compress_if_needed(path: str) -> str:
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb <= CONFIG["max_upload_size_mb"]:
        return path
    ensure_dir(CONFIG["download_dir"])
    out = path.replace(".mp3", "_comp.mp3")
    logger.info(f"דוחס קובץ {size_mb:.1f}MB")
    subprocess.run([
        "ffmpeg", "-y", "-i", path,
        "-ac", "1", "-ar", CONFIG["ffmpeg_sample_rate"],
        "-b:a", CONFIG["ffmpeg_bitrate"], out
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


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


def process_post(post: dict, state: dict):
    post_id = post["id"]
    title = post.get("title", {}).get("rendered", "")
    title = re.sub(r"<[^>]+>", "", title).strip()
    youtube_url = post.get("video_to_post", "").strip()

    if not youtube_url:
        logger.info(f"#{post_id} {title}: אין קישור YouTube — מדלג")
        state["last_id"] = post_id
        save_state(state)
        return

    tts_text = build_tts_text(title)
    song_path = None
    tts_path = None
    comp_path = None

    try:
        song_path = download_youtube_audio(youtube_url, post_id)
        comp_path = compress_if_needed(song_path)
        tts_path = create_tts_file(post_id, tts_text)

        # שיר קודם → TTS אחרון (ימות משמיע TTS ראשון)
        upload_to_yemot(comp_path)
        logger.info(f"#{post_id}: שיר הועלה")

        upload_to_yemot(tts_path)
        logger.info(f"#{post_id}: הכרזה הועלתה | {tts_text[:60]}")

    except subprocess.CalledProcessError as e:
        logger.error(f"#{post_id}: yt-dlp נכשל: {e.stderr.decode() if e.stderr else e}")
        return
    except Exception as e:
        logger.exception(f"#{post_id}: שגיאה: {e}")
        return
    finally:
        for p in {song_path, comp_path, tts_path}:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    state["last_id"] = post_id
    save_state(state)
    time.sleep(2)


def process_once():
    state = load_state()
    last_id = state.get("last_id", 0)
    initialized = state.get("initialized", False)

    logger.info("סורק hamenagen.net")
    try:
        posts = fetch_posts()
    except Exception as e:
        logger.exception(f"שגיאה בקריאת API: {e}")
        return

    if not posts:
        return

    if not initialized:
        sorted_posts = sorted(posts, key=lambda p: p["id"])
        backfill = sorted_posts[-3:]
        last_id = backfill[0]["id"] - 1
        state["last_id"] = last_id
        state["initialized"] = True
        save_state(state)
        logger.info(f"אותחל. מעבד {len(backfill)} שירים אחרונים")

    new_posts = sorted(
        [p for p in posts if p["id"] > last_id],
        key=lambda p: p["id"]
    )

    if not new_posts:
        logger.info("אין שירים חדשים")
        return

    logger.info(f"נמצאו {len(new_posts)} שירים חדשים")
    for post in new_posts:
        process_post(post, state)
        state = load_state()


def main():
    ensure_dir(CONFIG["tts_dir"])
    ensure_dir(CONFIG["download_dir"])
    get_api_key()
    logger.info("התחיל — שירים חדשים שלוחה 303")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
