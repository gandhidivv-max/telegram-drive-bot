import os
import sys
import random
import time
import requests
import json
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
APP_ID = os.environ.get("PINTEREST_APP_ID")
APP_SECRET = os.environ.get("PINTEREST_APP_SECRET")
REFRESH_TOKEN = os.environ.get("PINTEREST_REFRESH_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)

# Directories & State files
HISTORY_DIR = "history"
STATE_FILE = "bot_state.json"
DAILY_REPORT_FILE = "daily_report.json"

if not os.path.exists(HISTORY_DIR):
    os.makedirs(HISTORY_DIR)

# ==========================================
# 2. LOGGING & UTILITY FUNCTIONS
# ==========================================
def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")
    sys.stdout.flush()

def safe_api_request(method, url, **kwargs):
    """ Exponential Backoff Algorithm for 429 Anti-Block System """
    retries = 5
    delay = 1
    for i in range(retries):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code == 429:
                log(f"⚠️ Rate limited (429). Retrying in {delay}s...", level="WARNING")
                time.sleep(delay)
                delay *= 2
                continue
            return response
        except Exception as e:
            log(f"❌ Network Error: {e}. Retrying in {delay}s...", level="ERROR")
            time.sleep(delay)
            delay *= 2
    return None

# ==========================================
# 3. TELEGRAM COMMANDS & MEDIA SENDER
# ==========================================
def check_pause_status():
    """ Handles /pause and /resume commands via Telegram Updates """
    state = load_json(STATE_FILE, default={"last_board_index": -1, "is_paused": False})
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    res = safe_api_request("GET", url)
    if res and res.status_code == 200:
        updates = res.json().get("result", [])
        for update in updates:
            message = update.get("message", {})
            text = message.get("text", "").strip()
            if text == "/pause":
                state["is_paused"] = True
                save_json(STATE_FILE, state)
                log("⏸️ Bot PAUSED via Telegram command.", level="WARNING")
            elif text == "/resume":
                state["is_paused"] = False
                save_json(STATE_FILE, state)
                log("▶️ Bot RESUMED via Telegram command.", level="INFO")
                
    return state.get("is_paused", False)

def send_telegram_media(media_url, board_name, is_video=False, is_spoiler=False):
    """ Sends Image/Video with Board Name as Inline Button """
    endpoint = "sendVideo" if is_video else "sendPhoto"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{endpoint}"
    
    reply_markup = {
        "inline_keyboard": [[
            {"text": f"📁 Board: {board_name}", "callback_data": "board_info"}
        ]]
    }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        ("video" if is_video else "photo"): media_url,
        "protect_content": True,
        "has_spoiler": is_spoiler,
        "reply_markup": json.dumps(reply_markup)
    }

    res = safe_api_request("POST", url, data=payload)
    if res and res.status_code == 200:
        log(f"✅ Posted to Telegram successfully from '{board_name}'.")
        return True
    else:
        err_msg = res.text if res else "No Response"
        log(f"❌ Failed to post to Telegram: {err_msg}", level="ERROR")
        return False

def send_telegram_message(chat_id, text):
    """ Simple Text Message Sender for Daily Admin Report """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    safe_api_request("POST", url, data=payload)

# ==========================================
# 4. FAST O(1) HISTORY RECORD STORAGE
# ==========================================
def get_history_filepath(board_id):
    safe_id = "".join([c for c in board_id if c.isalnum() or c in ('-', '_')])
    return os.path.join(HISTORY_DIR, f"posted_{safe_id}.txt")

def load_posted_ids(board_id):
    filepath = get_history_filepath(board_id)
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def record_posted_id(board_id, item_id):
    filepath = get_history_filepath(board_id)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# ==========================================
# 5. PINTEREST API INTEGRATION
# ==========================================
def get_access_token():
    url = "https://api.pinterest.com/v5/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN
    }
    res = safe_api_request("POST", url, data=payload, auth=(APP_ID, APP_SECRET))
    if res and res.status_code == 200:
        log("✅ Access Token Refreshed!")
        return res.json().get("access_token")
    log("❌ Access Token Refresh Failed!", level="ERROR")
    return None

def fetch_boards_and_subboards(access_token):
    url = "https://api.pinterest.com/v5/boards"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = safe_api_request("GET", url, headers=headers)
    
    all_targets = []
    if res and res.status_code == 200:
        boards = res.json().get("items", [])
        for b in boards:
            if b["name"].strip().lower() == "quick saves":
                continue

            all_targets.append({
                "id": b["id"],
                "name": b["name"],
                "is_subboard": False
            })
            sub_url = f"https://api.pinterest.com/v5/boards/{b['id']}/sections"
            sub_res = safe_api_request("GET", sub_url, headers=headers)
            if sub_res and sub_res.status_code == 200:
                sub_boards = sub_res.json().get("items", [])
                for sb in sub_boards:
                    all_targets.append({
                        "id": sb["id"],
                        "name": f"{b['name']} > {sb['name']}",
                        "is_subboard": True,
                        "parent_id": b["id"]
                    })
                    
    all_targets.sort(key=lambda x: x["name"].lower())
    log(f"📋 Total Boards/Sub-boards Detected & Sorted: {len(all_targets)}")
    return all_targets

def fetch_pins_from_board(access_token, target):
    headers = {"Authorization": f"Bearer {access_token}"}
    if target["is_subboard"]:
        url = f"https://api.pinterest.com/v5/boards/{target['parent_id']}/sections/{target['id']}/pins"
    else:
        url = f"https://api.pinterest.com/v5/boards/{target['id']}/pins"
        
    res = safe_api_request("GET", url, headers=headers)
    if res and res.status_code == 200:
        return res.json().get("items", [])
    return []

# ==========================================
# 6. MAIN ENGINE WORKFLOW
# ==========================================
def main():
    log("🚀 Script execution started.")

    # 1. Check Pause Status
    if check_pause_status():
        log("⏸️ Execution skipped. Bot is currently PAUSED.", level="WARNING")
        return

    # 2. Get Access Token
    access_token = get_access_token()
    if not access_token:
        return

    # 3. Fetch Boards
    targets = fetch_boards_and_subboards(access_token)
    if not targets:
        log("⚠️ No valid public boards found.", level="WARNING")
        return

    # Round-Robin State Tracking
    state = load_json(STATE_FILE, default={"last_board_index": -1, "is_paused": False})
    last_idx = state.get("last_board_index", -1)
    
    start_index = (last_idx + 1) % len(targets)

    posted = False
    report_data = load_json(DAILY_REPORT_FILE, default={"images": 0, "videos": 0, "boards": {}, "last_report_date": ""})

    for idx_offset in range(len(targets)):
        current_idx = (start_index + idx_offset) % len(targets)
        target = targets[current_idx]
        board_id = target["id"]
        board_name = target["name"]

        log(f"🔍 Checking Board [{current_idx + 1}/{len(targets)}]: {board_name}")

        posted_ids = load_posted_ids(board_id)
        pins = fetch_pins_from_board(access_token, target)

        if not pins:
            log(f"⏩ Board '{board_name}' has no pins or finished. Skipping.")
            continue

        random.shuffle(pins)

        for pin in pins:
            pin_id = pin.get("id")
            if pin_id in posted_ids:
                continue

            media = pin.get("media", {})
            media_type = media.get("media_type", "image")
            
            media_url = None
            is_video = False

            if media_type == "video":
                is_video = True
                media_url = media.get("video_list", {}).get("V_720P", {}).get("url")
            else:
                images = media.get("images", {})
                media_url = images.get("600x", {}).get("url") or images.get("originals", {}).get("url")

            if not media_url:
                continue

            is_spoiler = "special" in board_name.lower()
            if is_spoiler:
                log(f"🔞 Board name contains 'special'. Enabling Blur (Spoiler).")

            log(f"📤 Posting media {pin_id} from '{board_name}'...")
            success = send_telegram_media(media_url, board_name=board_name, is_video=is_video, is_spoiler=is_spoiler)

            if success:
                record_posted_id(board_id, pin_id)
                
                state["last_board_index"] = current_idx
                save_json(STATE_FILE, state)

                if is_video:
                    report_data["videos"] = report_data.get("videos", 0) + 1
                else:
                    report_data["images"] = report_data.get("images", 0) + 1

                boards_dict = report_data.get("boards", {})
                boards_dict[board_name] = boards_dict.get(board_name, 0) + 1
                report_data["boards"] = boards_dict
                
                save_json(DAILY_REPORT_FILE, report_data)

                posted = True
                break

        if posted:
            break

    # ==========================================
    # 4. DYNAMIC DAILY REPORT (8:00 PM IST Trigger)
    # ==========================================
    # Convert UTC time directly to IST (UTC + 5:30)
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    
    today_str = ist_now.strftime("%Y-%m-%d")
    last_report_date = report_data.get("last_report_date", "")

    # IST టైమ్ ప్రకారం రాత్రి 8 గంటలు (20:00) దాటితే మరియు ఈరోజుకి ఇంకా రిపోర్ట్ పంపకపోతే...
    if ist_now.hour >= 20 and last_report_date != today_str:
        total_posts = report_data.get("images", 0) + report_data.get("videos", 0)
        
        if total_posts > 0:
            summary_text = (
                f"📊 <b>Daily Pinterest Analytics Summary Report</b>\n"
                f"📅 Date: {today_str}\n\n"
                f"🖼 Total Images Posted: <b>{report_data.get('images', 0)}</b>\n"
                f"🎥 Total Videos Posted: <b>{report_data.get('videos', 0)}</b>\n\n"
                f"<b>Board Details:</b>\n"
            )
            for b_name, count in report_data.get("boards", {}).items():
                summary_text += f"• {b_name}: {count} post(s)\n"

            send_telegram_message(ADMIN_CHAT_ID, summary_text)
            log("📈 Daily Analytics Report sent to Admin!")

        # ఈరోజు రిపోర్ట్ పంపినట్లు మార్క్ చేసి, కౌంటర్‌ని ఆటోమేటిక్‌గా రీసెట్ చేయడం
        new_report_data = {
            "images": 0, 
            "videos": 0, 
            "boards": {}, 
            "last_report_date": today_str
        }
        save_json(DAILY_REPORT_FILE, new_report_data)

if __name__ == "__main__":
    main()
    
