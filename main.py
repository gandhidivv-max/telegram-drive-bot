import os
import sys
import random
import time
import requests
import json
from datetime import datetime

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
    state = load_json(STATE_FILE, default={"is_paused": False})
    
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

def send_telegram_media(media_url, is_video=False, is_spoiler=False):
    """ Sends Image/Video with Inline Buttons, Content Protection, and optional Blur """
    endpoint = "sendVideo" if is_video else "sendPhoto"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{endpoint}"
    
    # Inline Reactions Keyboard
    reply_markup = {
        "inline_keyboard": [[
            {"text": "❤️", "callback_data": "like"},
            {"text": "🔥", "callback_data": "fire"},
            {"text": "👍", "callback_data": "thumbs_up"}
        ]]
    }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        ("video" if is_video else "photo"): media_url,
        "protect_content": True,  # Disables saving & forwarding
        "has_spoiler": is_spoiler, # Blur effect if True
        "reply_markup": json.dumps(reply_markup)
    }

    res = safe_api_request("POST", url, data=payload)
    if res and res.status_code == 200:
        log("✅ Posted to Telegram successfully (No text added).")
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
    """ Fast Hash Set lookup O(1) for high performance even with millions of IDs """
    filepath = get_history_filepath(board_id)
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def record_posted_id(board_id, item_id):
    """ Appends posted ID to dedicated board text file """
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
    """ Auto-detects main boards & sub-boards, sorted alphabetically """
    url = "https://api.pinterest.com/v5/boards"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = safe_api_request("GET", url, headers=headers)
    
    all_targets = []
    if res and res.status_code == 200:
        boards = res.json().get("items", [])
        for b in boards:
            # Skip Pinterest default system board "Quick saves"
            if b["name"].strip().lower() == "quick saves":
                continue

            all_targets.append({
                "id": b["id"],
                "name": b["name"],
                "is_subboard": False
            })
            # Check for Sub-boards
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
                    
    # Alphabetical Order (Ascending)
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

    # 2. Get Fresh Access Token
    access_token = get_access_token()
    if not access_token:
        return

    # 3. Fetch All Boards & Subboards
    targets = fetch_boards_and_subboards(access_token)
    if not targets:
        log("⚠️ No valid public boards found.", level="WARNING")
        return

    # Round-Robin State Tracking
    state = load_json(STATE_FILE, default={"last_board_index": -1})
    start_index = (state.get("last_board_index", -1) + 1) % len(targets)

    posted = False
    report_data = load_json(DAILY_REPORT_FILE, default={"images": 0, "videos": 0, "boards": {}})

    # Sequential iteration through boards starting from last left off
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

        # Shuffle pins randomly for random selection
        random.shuffle(pins)

        # Select first unposted item
        for pin in pins:
            pin_id = pin.get("id")
            if pin_id in posted_ids:
                continue # Fast O(1) check

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

            # Check if Blur effect is needed ONLY for "special" board
            is_spoiler = "special" in board_name.lower()
            if is_spoiler:
                log(f"🔞 Board name contains 'special'. Enabling Blur (Spoiler).")

            log(f"📤 Posting media {pin_id} from '{board_name}'...")
            success = send_telegram_media(media_url, is_video=is_video, is_spoiler=is_spoiler)

            if success:
                record_posted_id(board_id, pin_id)
                
                state["last_board_index"] = current_idx
                save_json(STATE_FILE, state)

                if is_video:
                    report_data["videos"] += 1
                else:
                    report_data["images"] += 1

                report_data["boards"][board_name] = report_data["boards"].get(board_name, 0) + 1
                save_json(DAILY_REPORT_FILE, report_data)

                posted = True
                break

        if posted:
            break

    # 4. Daily Digest Report (Triggers at 8 PM / 20:00)
    now = datetime.now()
    if now.hour == 20 and (report_data.get("images", 0) + report_data.get("videos", 0) > 0):
        summary_text = (
            f"📊 <b>Daily Pinterest Analytics Summary Report</b>\n"
            f"📅 Date: {now.strftime('%Y-%m-%d')}\n\n"
            f"🖼 Total Images Posted: <b>{report_data.get('images', 0)}</b>\n"
            f"🎥 Total Videos Posted: <b>{report_data.get('videos', 0)}</b>\n\n"
            f"<b>Board Details:</b>\n"
        )
        for b_name, count in report_data.get("boards", {}).items():
            summary_text += f"• {b_name}: {count} post(s)\n"

        send_telegram_message(ADMIN_CHAT_ID, summary_text)
        log("📈 Daily Analytics Report sent to Admin!")
        
        # Reset report tracking for next day
        save_json(DAILY_REPORT_FILE, {"images": 0, "videos": 0, "boards": {}})

if __name__ == "__main__":
    main()
                                        
