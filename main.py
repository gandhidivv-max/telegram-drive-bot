import os
import json
import time
import random
import logging
import requests
from datetime import datetime
import pytz

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

# Fetch Basic Secrets
PINTEREST_APP_ID = os.getenv("PINTEREST_APP_ID")
PINTEREST_APP_SECRET = os.getenv("PINTEREST_APP_SECRET")
PINTEREST_REFRESH_TOKEN = os.getenv("PINTEREST_REFRESH_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

STATE_FILE = "state.json"
CONTROL_FILE = "bot_control.json"
STATS_FILE = "daily_stats.json"

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading {filepath}: {e}")
            return default
    return default

def save_json(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
        logging.info(f"Saved {filepath}")
    except Exception as e:
        logging.error(f"Error saving {filepath}: {e}")

def load_posted_ids(board_id):
    file_path = f"posted_{board_id}.txt"
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def append_posted_id(board_id, item_id):
    file_path = f"posted_{board_id}.txt"
    with open(file_path, "a") as f:
        f.write(f"{item_id}\n")

def send_admin_report(report_text):
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_CHAT_ID, "text": report_text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Failed to send admin report: {e}")

def api_request_with_backoff(url, method="GET", headers=None, data=None, json_payload=None, auth=None):
    max_retries = 5
    delay = 1
    for attempt in range(max_retries):
        try:
            if method == "GET":
                res = requests.get(url, headers=headers, auth=auth, timeout=15)
            else:
                res = requests.post(url, headers=headers, data=data, json=json_payload, auth=auth, timeout=15)

            if res is not None and res.status_code == 429:
                logging.warning(f"Rate limited (429). Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            return res
        except Exception as e:
            logging.error(f"Request Exception on {url}: {e}")
            time.sleep(delay)
            delay *= 2
    return None

def get_pinterest_access_token():
    url = "https://api.pinterest.com/v5/oauth/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": PINTEREST_REFRESH_TOKEN,
    }
    res = api_request_with_backoff(url, method="POST", headers=headers, data=data, auth=(PINTEREST_APP_ID, PINTEREST_APP_SECRET))
    
    if res and res.ok:
        return res.json().get("access_token")
    
    err_msg = res.text if res else "No Response"
    logging.critical(f"Pinterest Token Error: {err_msg}")
    send_admin_report(f"🚨 **CRITICAL ALERT**: Pinterest Token Error!\n`{err_msg}`")
    return None

def get_all_user_boards(access_token):
    url = "https://api.pinterest.com/v5/boards"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = api_request_with_backoff(url, headers=headers)
    
    boards_dict = {}
    if res and res.ok:
        items = res.json().get("items", [])
        for item in items:
            b_id = str(item.get("id"))
            b_name = str(item.get("name"))
            boards_dict[b_id] = b_name
        logging.info(f"Auto-scanned {len(boards_dict)} boards from Pinterest account.")
    else:
        err_msg = res.text if res else "No Response"
        logging.error(f"Failed to fetch boards automatically: {err_msg}")
        
    return boards_dict

def get_pins_from_target(access_token, target_id):
    url = f"https://api.pinterest.com/v5/boards/{target_id}/pins"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = api_request_with_backoff(url, headers=headers)
    if res and res.ok:
        return res.json().get("items", [])
    return []

def get_single_pin_details(access_token, pin_id):
    """పిన్ యొక్క పూర్తి వివరాలు (Media Video URLs తో సహా) పొందడానికి సపరేట్ API కాల్"""
    url = f"https://api.pinterest.com/v5/pins/{pin_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = api_request_with_backoff(url, headers=headers)
    if res and res.ok:
        return res.json()
    return {}

def extract_video_url_from_pin_data(pin_details):
    media = pin_details.get("media", {})
    media_type = media.get("media_type", "")
    
    logging.info(f"Media Type for Pin {pin_details.get('id')}: {media_type}")
    
    videos_dict = media.get("videos", {}) or media.get("video", {})
    video_list = videos_dict.get("video_list", {}) if isinstance(videos_dict, dict) else {}

    if video_list or media_type in ["video", "multiple_videos"]:
        for v_key in ["V_720P", "V_EXP4", "V_EXP3", "V_480P", "V_360P"]:
            if v_key in video_list and isinstance(video_list[v_key], dict) and "url" in video_list[v_key]:
                return video_list[v_key]["url"]

        for v_obj in video_list.values():
            if isinstance(v_obj, dict) and "url" in v_obj:
                return v_obj["url"]

    if "video_url" in media and media["video_url"]:
        return media["video_url"]

    return None

def post_video_to_telegram(media_url, board_name):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    reply_markup = {
        "inline_keyboard": [[{"text": f"📌 Board: {board_name}", "callback_data": "ignore_click"}]]
    }
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "video": media_url,
        "protect_content": True,
        "reply_markup": json.dumps(reply_markup)
    }

    res = api_request_with_backoff(url, method="POST", json_payload=payload)
    if res and res.ok:
        return True
    
    err_text = res.text if res else "No Response"
    logging.error(f"Telegram Video Posting Failed: {err_text}")
    send_admin_report(f"❌ **Telegram Post Error**:\n`{err_text}`")
    return False

def main():
    logging.info("Starting Detailed Video Scan Mode...")

    access_token = get_pinterest_access_token()
    if not access_token:
        return

    board_mapping = get_all_user_boards(access_token)
    if not board_mapping:
        return

    sorted_board_ids = sorted(board_mapping.keys(), key=lambda b_id: str(board_mapping[b_id]).lower())
    posted_successfully = False

    for board_id in sorted_board_ids:
        board_name = str(board_mapping[board_id])
        posted_ids = load_posted_ids(board_id)

        # 1. Get Board Pins
        all_pins = get_pins_from_target(access_token, board_id)
        
        # posted_ids లో లేనివి వెతకడం
        unposted = [p for p in all_pins if str(p.get("id")) not in posted_ids]

        logging.info(f"Scanning Board '{board_name}': Found {len(all_pins)} total pins, {len(unposted)} unposted pins.")

        for p_summary in unposted:
            pin_id = str(p_summary.get("id"))
            
            # 2. Get Single Pin Full Data (To get accurate media dict)
            pin_details = get_single_pin_details(access_token, pin_id)
            video_url = extract_video_url_from_pin_data(pin_details)

            if video_url:
                logging.info(f"🎥 Found Video URL for Pin {pin_id}! Posting to Telegram...")
                if post_video_to_telegram(video_url, board_name):
                    append_posted_id(board_id, pin_id)
                    send_admin_report(f"✅ **Video Posted Successfully!**\nBoard: `{board_name}`\nPin ID: `{pin_id}`")
                    posted_successfully = True
                    break
                else:
                    send_admin_report(f"⚠️ Video found for Pin `{pin_id}`, but Telegram upload failed.")
            else:
                logging.info(f"Pin {pin_id} is an IMAGE or has no video stream. Skipping...")

        if posted_successfully:
            break

    if not posted_successfully:
        send_admin_report("⚠️ **Test Finished**: Scanned boards, but NO video pins were found among unposted pins.")

if __name__ == "__main__":
    main()
