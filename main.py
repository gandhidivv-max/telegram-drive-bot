import os
import json
import time
import random
import logging
import requests
import re
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

# Helper Functions
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

def check_telegram_commands():
    if not ADMIN_BOT_TOKEN:
        return True
    
    url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/getUpdates"
    res = api_request_with_backoff(url)
    control = load_json(CONTROL_FILE, {"is_paused": False, "last_update_id": 0})
    
    if res and res.ok:
        updates = res.json().get("result", [])
        for update in updates:
            control["last_update_id"] = update["update_id"]
            message = update.get("message", {})
            text = message.get("text", "").strip().lower()
            
            if text == "/pause":
                control["is_paused"] = True
                send_admin_report("⏸️ Bot execution has been **PAUSED** via Telegram command.")
            elif text == "/resume":
                control["is_paused"] = False
                send_admin_report("▶️ Bot execution has been **RESUMED** via Telegram command.")
                
        save_json(CONTROL_FILE, control)
    
    return not control.get("is_paused", False)

# Pinterest API
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
        send_admin_report(f"❌ **Auto Board Fetch Failed**: `{err_msg}`")
        
    return boards_dict

def get_board_sections(access_token, board_id):
    url = f"https://api.pinterest.com/v5/boards/{board_id}/sections"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = api_request_with_backoff(url, headers=headers)
    if res and res.ok:
        return [sec["id"] for sec in res.json().get("items", [])]
    return []

def get_pins_from_target(access_token, target_id, is_section=False):
    fields_query = "?pin_filter=exclude_native&fields=id,title,description,link,media"
    
    if is_section:
        url = f"https://api.pinterest.com/v5/boards/sections/{target_id}/pins{fields_query}"
    else:
        url = f"https://api.pinterest.com/v5/boards/{target_id}/pins{fields_query}"
        
    headers = {"Authorization": f"Bearer {access_token}"}
    res = api_request_with_backoff(url, headers=headers)
    if res and res.ok:
        return res.json().get("items", [])
    return []

def scrape_video_url_from_web(pin_id):
    """API వీడియో లింక్ ఇవ్వనప్పుడు వెబ్ పేజీ నుండి MP4 లింక్‌ను పట్టుకునే బైపాస్ లాజిక్"""
    try:
        web_url = f"https://www.pinterest.com/pin/{pin_id}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
        res = requests.get(web_url, headers=headers, timeout=10)
        if res.ok:
            matches = re.findall(r'https://v1\.pinimg\.com/videos/[^\s"\'\\]+\.mp4', res.text)
            if not matches:
                matches = re.findall(r'https://[^\s"\'\\]+\.mp4', res.text)
            if matches:
                logging.info(f"🎉 Web Scraping Bypass SUCCESS for Pin {pin_id}! Video URL found.")
                return matches[0]
    except Exception as e:
        logging.error(f"Scraping error for pin {pin_id}: {e}")
    return None

def extract_media_info(pin):
    pin_id = str(pin.get("id"))
    media = pin.get("media", {})
    media_type = media.get("media_type", "")
    
    # 1. API Direct Video check
    if media_type in ["video", "multiple_videos"]:
        video_list = media.get("videos", {}).get("video_list", {})
        if not video_list and "video" in media:
            video_list = media.get("video", {}).get("video_list", {})

        for v_key in ["V_720P", "V_EXP4", "V_EXP3", "V_480P", "V_360P"]:
            if v_key in video_list and "url" in video_list[v_key]:
                return video_list[v_key]["url"], True
        
        for v_obj in video_list.values():
            if isinstance(v_obj, dict) and "url" in v_obj:
                return v_obj["url"], True

    # 2. Web Scraping Bypass Check (API కేవలం Image అని చెప్పినా పేజీలో MP4 ఉందేమో వెతుకుతుంది)
    scraped_video_url = scrape_video_url_from_web(pin_id)
    if scraped_video_url:
        return scraped_video_url, True

    # 3. Standard Image Fallback (వీడియో లేకపోతే ఇమేజ్ పోస్ట్ అవుతుంది)
    images = media.get("images", {})
    if not images and "media" in pin:
        images = pin.get("media", {}).get("images", {})
        
    for i_key in ["originals", "1200x", "600x", "400x300"]:
        if i_key in images and "url" in images[i_key]:
            return images[i_key]["url"], False
            
    if "image_large_url" in pin:
        return pin["image_large_url"], False

    return None, False

def post_media_to_telegram(media_url, is_video, is_special_board, board_name):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/" + ("sendVideo" if is_video else "sendPhoto")
    
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": f"📌 Board: {board_name}",
                    "callback_data": "ignore_click"
                }
            ]
        ]
    }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        ("video" if is_video else "photo"): media_url,
        "protect_content": True,
        "reply_markup": json.dumps(reply_markup)
    }
    
    if is_special_board:
        payload["has_spoiler"] = True

    res = api_request_with_backoff(url, method="POST", json_payload=payload)
    if res and res.ok:
        return True
    
    err_text = res.text if res else "No Response"
    logging.error(f"Telegram Posting Failed: {err_text}")
    send_admin_report(f"❌ **Telegram Post Error**:\n`{err_text}`")
    return False

# Main Process
def main():
    logging.info("Starting Auto-Board Scan Pinterest Engine with Web Scraping Bypass...")

    if not check_telegram_commands():
        logging.info("Bot is PAUSED. Exiting.")
        return

    access_token = get_pinterest_access_token()
    if not access_token:
        return

    board_mapping = get_all_user_boards(access_token)

    if not board_mapping:
        logging.warning("No boards fetched from Pinterest account.")
        return

    # Sort Boards A to Z by Name
    sorted_board_ids = sorted(board_mapping.keys(), key=lambda b_id: str(board_mapping[b_id]).lower())

    state = load_json(STATE_FILE, {"board_index": 0, "last_report_date": ""})
    stats = load_json(STATS_FILE, {})

    total_boards = len(sorted_board_ids)
    start_index = state.get("board_index", 0) % total_boards

    posted_successfully = False

    for i in range(total_boards):
        curr_index = (start_index + i) % total_boards
        board_id = sorted_board_ids[curr_index]
        board_name = str(board_mapping[board_id])

        is_special_board = (board_name.strip().lower() == "special")
        posted_ids = load_posted_ids(board_id)

        all_pins = get_pins_from_target(access_token, board_id, is_section=False)
        sub_sections = get_board_sections(access_token, board_id)
        for sec_id in sub_sections:
            all_pins.extend(get_pins_from_target(access_token, sec_id, is_section=True))

        unposted = [p for p in all_pins if str(p.get("id")) not in posted_ids]

        if not unposted:
            logging.info(f"Board '{board_name}' ({board_id}) has no new items. Skipping...")
            continue

        random.shuffle(unposted)
        
        for selected_pin in unposted:
            pin_id = str(selected_pin.get("id"))
            media_url, is_video = extract_media_info(selected_pin)

            if not media_url:
                continue

            if post_media_to_telegram(media_url, is_video, is_special_board, board_name):
                # 🛠️ లాగ్స్‌లో Board ID కూడా స్పష్టంగా కనిపిస్తుంది
                logging.info(f"Successfully posted {'VIDEO' if is_video else 'IMAGE'} Pin {pin_id} from Board '{board_name}' ({board_id})")
                
                append_posted_id(board_id, pin_id)
                state["board_index"] = (curr_index + 1) % total_boards
                posted_successfully = True

                today = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
                if today not in stats:
                    stats[today] = {}
                if board_name not in stats[today]:
                    stats[today][board_name] = {"images": 0, "videos": 0}

                if is_video:
                    stats[today][board_name]["videos"] += 1
                else:
                    stats[today][board_name]["images"] += 1

                break

        if posted_successfully:
            break

    if not posted_successfully:
        logging.info("No media posted in this run.")

    save_json(STATE_FILE, state)
    save_json(STATS_FILE, stats)

    # Daily Report at 8 PM IST
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")

    if now_ist.hour >= 20 and state.get("last_report_date") != today_str:
        today_data = stats.get(today_str, {})
        report_msg = f"📊 *Daily Analytics Summary Report ({today_str})*\n\n"
        
        total_img, total_vid = 0, 0
        if today_data:
            for b_name, counts in today_data.items():
                img, vid = counts["images"], counts["videos"]
                total_img += img
                total_vid += vid
                report_msg += f"🔹 *{b_name}*: {img} Images, {vid} Videos\n"
        else:
            report_msg += "No posts published today.\n"

        report_msg += f"\n✅ *Total Summary*: {total_img} Images | {total_vid} Videos"
        
        send_admin_report(report_msg)
        state["last_report_date"] = today_str
        save_json(STATE_FILE, state)

if __name__ == "__main__":
    main()
                
