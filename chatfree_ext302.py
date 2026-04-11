#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# שלוחה 302 — בחורי ישיבות (yeshiva-zucher.chatfree.app)

import asyncio
import os
import re
import json
import time
import logging
import mimetypes
from datetime import datetime, timezone, timedelta

import subprocess

import requests
import edge_tts
from curl_cffi import requests as cffi_requests
from hebrew_time import time_to_hebrew

DATA_DIR = os.environ.get("DATA_DIR", ".")

CONFIG = {
    "target_extension": "302",
    "yemot_private": False,
    "convert_audio": True,
    "api_url": "https://yeshiva-zucher.chatfree.app/api/messages",
    "check_interval_seconds": 120,
    "tts_dir": os.path.join(DATA_DIR, "tts_yeshiva"),
    "video_dir": os.path.join(DATA_DIR, "vid_yeshiva"),
    "state_file": os.path.join(DATA_DIR, "state_yeshiva.json"),
    "timeout": 30,
    "tts_voice": "he-IL-AvriNeural",
    "max_upload_size_mb": 45,
    "ffmpeg_bitrate": "48k",
    "ffmpeg_sample_rate": "22050",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("yeshiva-ext302")


def get_api_key() -> str:
    key = os.environ.get("YEMOT_API_KEY", "").strip().strip("'\"׳״` ")
    if not key:
        raise RuntimeError("לא נמצא YEMOT_API_KEY")
    key.encode("latin-1")
    return key


def get_session_cookie() -> str:
    return os.environ.get("CHATFREE_SESSION", "MTc3NTM4MDYxMXxOd3dBTkRWRk4wdFdOMUpXTTFkT1draElUMWcwV1ZkWVFqVkJWMWczVkVaSVdGcEJWRmhNVlUxWlZVRkxUVE0wUVVsR1IxZEdWVkU9fFMGF-KzTTwRdhIfPU0HmFErxXLXJrbuzM4aflhSFLoT")


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


def fetch_messages() -> list:
    params = {"offset": 0, "limit": 20, "direction": "desc"}
    headers = {
        "accept": "application/json, text/plain, */*",
        "referer": "https://yeshiva-zucher.chatfree.app/",
        "cookie": f"channel_session={get_session_cookie()}",
    }
    r = cffi_requests.get(
        CONFIG["api_url"], params=params, headers=headers,
        impersonate="chrome110", timeout=CONFIG["timeout"]
    )
    r.raise_for_status()
    return r.json().get("messages", [])


def parse_timestamp(ts: str) -> str:
    """המרת UTC timestamp לשעת ישראל HH:MM"""
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        il_time = dt.astimezone(timezone(timedelta(hours=3)))
        return il_time.strftime("%H:%M")
    except Exception:
        return ""


def clean_text(text: str) -> str:
    # הסרת תמונות/וידאו embedded
    text = re.sub(r'\[(?:image|video)-embedded#\]\([^)]+\)', '', text)
    # הסרת הפוטר החוזר
    text = re.split(r"'ישיב'ע זוכע'ר' - סקופים בלעדיים", text)[0]
    # הסרת קידומת "ישיב'ע זוכע'ר: " בתחילת ההודעה
    text = re.sub(r"^\S+\s+\S+:\s*", "", text)
    # הסרת markdown bold
    text = text.replace('*', '')
    # הסרת קישורים
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'chat\.whatsapp\.com/\S+', '', text)
    text = re.sub(r'wa\.me/\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:500]


def extract_video_url(text: str) -> str:
    """מחלץ URL של וידאו מתוך הודעה"""
    m = re.search(r'\[video-embedded#\]\(([^)]+)\)', text)
    if m:
        return m.group(1).strip()
    m = re.search(r'(https?://\S+\.(?:mp4|mov|avi|webm|mkv))', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def compress_if_needed(path: str) -> str:
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb <= CONFIG["max_upload_size_mb"]:
        return path
    out = path.replace(".mp3", "_comp.mp3")
    logger.info(f"דוחס {size_mb:.1f}MB")
    subprocess.run([
        "ffmpeg", "-y", "-i", path,
        "-ac", "1", "-ar", CONFIG["ffmpeg_sample_rate"],
        "-b:a", CONFIG["ffmpeg_bitrate"], out
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def download_video_audio(url: str, msg_id: int) -> str:
    """מוריד וידאו ומחלץ ממנו אודיו"""
    ensure_dir(CONFIG["video_dir"])
    out_path = os.path.join(CONFIG["video_dir"], f"vid_{msg_id}.mp3")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    logger.info(f"מוריד וידאו: {url[:70]}")

    # נסיון עם yt-dlp (מתאים ל-YouTube + קישורים ישירים רבים)
    try:
        cmd = [
            "yt-dlp", "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "128K", "--no-playlist",
            "--output", out_path, "--no-progress",
            "--extractor-args", "youtube:player_client=android,web",
        ]
        cmd.append(url)
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=180)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        logger.warning(f"yt-dlp נכשל, מנסה הורדה ישירה: {e}")

    # fallback: הורדה ישירה + ffmpeg
    tmp = os.path.join(CONFIG["video_dir"], f"vid_{msg_id}.tmp")
    r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    subprocess.run([
        "ffmpeg", "-y", "-i", tmp,
        "-vn", "-ac", "1", "-ar", CONFIG["ffmpeg_sample_rate"],
        "-b:a", CONFIG["ffmpeg_bitrate"], out_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(tmp)
    except Exception:
        pass
    return out_path


def build_tts_text(msg: dict) -> str:
    t = time_to_hebrew(parse_timestamp(msg.get("timestamp", "")))
    content = clean_text(msg.get("text", ""))
    prefix = f"{t} " if t else ""
    return f"{prefix}במוקד עולם התורה {content}"


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

    logger.info(f"בודק הודעות ישיבות מ-ID: {last_id}")
    try:
        messages = fetch_messages()
    except Exception as e:
        logger.exception(f"שגיאה בקריאת API: {e}")
        return

    if not messages:
        return

    if not initialized:
        sorted_msgs = sorted(messages, key=lambda m: m["id"])
        backfill = sorted_msgs[-5:]
        min_backfill_id = backfill[0]["id"] - 1
        state["last_id"] = min_backfill_id
        last_id = min_backfill_id
        state["initialized"] = True
        save_state(state)
        logger.info(f"אותחל. מעבד {len(backfill)} הודעות אחרונות")

    new_messages = sorted([m for m in messages if m["id"] > last_id], key=lambda m: m["id"])

    if not new_messages:
        logger.info("אין הודעות חדשות")
        return

    logger.info(f"נמצאו {len(new_messages)} הודעות חדשות")

    for msg in new_messages:
        if msg.get("deleted"):
            continue

        msg_id = msg["id"]
        raw_text = msg.get("text", "")
        video_url = extract_video_url(raw_text)
        text = clean_text(raw_text)

        if not text and not video_url:
            continue

        tts_text = build_tts_text(msg) if text else None
        vid_path = None
        tts_path = None

        try:
            # וידאו קודם — בימות הראשון שמור ראשון
            if video_url:
                try:
                    vid_path = download_video_audio(video_url, msg_id)
                    vid_path = compress_if_needed(vid_path)
                    upload_to_yemot(vid_path)
                    logger.info(f"#{msg_id}: וידאו הועלה")
                except Exception as e:
                    logger.warning(f"#{msg_id}: וידאו נכשל (ממשיך ל-TTS): {e}")

            # אחר כך TTS
            if tts_text:
                tts_path = create_tts_file(msg_id, tts_text)
                upload_to_yemot(tts_path)
                logger.info(f"#{msg_id}: TTS הועלה | {tts_text[:60]}")

        except Exception as e:
            logger.exception(f"שגיאה #{msg_id}: {e}")
            continue
        finally:
            for p in [vid_path, tts_path]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

        state["last_id"] = msg_id
        save_state(state)
        time.sleep(1)


def main():
    ensure_dir(CONFIG["tts_dir"])
    ensure_dir(CONFIG["video_dir"])
    get_api_key()
    logger.info("התחיל — בחורי ישיבות שלוחה 302")
    logger.info(f"בדיקה כל {CONFIG['check_interval_seconds']} שניות")
    while True:
        try:
            process_once()
        except Exception as e:
            logger.exception(f"שגיאה: {e}")
        time.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    main()
