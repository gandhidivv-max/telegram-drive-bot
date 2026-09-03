import os
import json
import requests
from datetime import datetime
import pytz

# Secrets from GitHub Environment
PINTEREST_APP_ID = os.getenv("PINTEREST_APP_ID")
PINTEREST_APP_SECRET = os.getenv("PINTEREST_APP_SECRET")
PINTEREST_REFRESH_TOKEN = os.getenv("PINTEREST_REFRESH_TOKEN")

# Channel Telegram Details (Main Channel Bot)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Personal Admin Telegram Details (Admin Private Bot)
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

STATE_FILE = "state.json"
POSTED_PINS_FILE = "posted_pins.json"

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=4)

def get_pinterest_access_token():
    url = "https://api.pinterest.com/v5/oauth/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": PINTEREST_REFRESH_TOKEN,
    }
    res = requests.post(url, headers=headers, data=data, auth=(PINTEREST_APP_ID, PINTEREST_APP_SECRET))
    if res.ok:
        return res.json().get("access_token")
    print(f"Failed to get access token: {res.text}")
    return None

def get_user_boards(access_token):
    url = "https://api.pinterest.com/v5/boards"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = requests.get(url, headers=headers)
    if res.ok:
        return [board["id"] for board in res.json().get("items", [])]
    return []

def get_pins_from_board(access_token, board_id):
    url = f"https://api.pinterest.com/v5/boards/{board_id}/pins"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = requests.get(url, headers=headers)
    if res.ok:
        return res.json().get("items", [])
    return []

def send_telegram_photo(image_url, caption):
    """Main Channel కి మాత్రమే ఫోటో పోస్ట్ చేసే ఫంక్షన్"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": caption
    }
    res = requests.post(url, json=payload)
    return res.ok

def send_admin_report(report_text):
    """Admin Bot ద్వారా ప్రైవేట్‌గా మీకు మాత్రమే రిపోర్ట్ పంపే ఫంక్షన్"""
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        print("Admin Bot secrets are missing. Skipping admin report.")
        return
    
    url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": report_text,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload)
        if not res.ok:
            print(f"Failed to send Admin Report: {res.text}")
    except Exception as e:
        print(f"Error sending Admin Report: {e}")

def main():
    state = load_json(STATE_FILE, {"board_index": 0, "last_report_date": ""})
    posted_pins = set(load_json(POSTED_PINS_FILE, []))

    access_token = get_pinterest_access_token()
    if not access_token:
        return

    boards = get_user_boards(access_token)
    if not boards:
        print("No boards found.")
        return

    total_boards = len(boards)
    start_index = state.get("board_index", 0) % total_boards
    
    posted_successfully = False
    
    # Round-Robin Traversal
    for i in range(total_boards):
        current_board_index = (start_index + i) % total_boards
        board_id = boards[current_board_index]
        pins = get_pins_from_board(access_token, board_id)

        for pin in pins:
            pin_id = pin.get("id")
            if pin_id in posted_pins:
                continue

            media = pin.get("media", {})
            images = media.get("images", {})
            image_url = images.get("originals", {}).get("url") or images.get("600x", {}).get("url")

            if image_url:
                caption = pin.get("title") or pin.get("description") or ""
                if send_telegram_photo(image_url, caption):
                    print(f"Posted Pin ID {pin_id} from Board Index {current_board_index}")
                    posted_pins.add(pin_id)
                    
                    # Next Board Index Update
                    state["board_index"] = (current_board_index + 1) % total_boards
                    posted_successfully = True
                    break

        if posted_successfully:
            break

    # Save State & Posted Pins locally
    save_json(STATE_FILE, state)
    save_json(POSTED_PINS_FILE, list(posted_pins))

    # Daily Analytics Report (IST రాత్రి 8 PM దాటాక)
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")

    if now_ist.hour >= 20 and state.get("last_report_date") != today_str:
        report_msg = (
            f"📊 *Daily Analytics Summary ({today_str})*\n\n"
            f"✅ Total Unique Pins Posted: {len(posted_pins)}\n"
            f"🔄 Next Board Index: {state.get('board_index')}\n"
            f"🚀 Bot Status: Working smoothly without duplicate board repetition!"
        )
        send_admin_report(report_msg)
        state["last_report_date"] = today_str
        save_json(STATE_FILE, state)

if __name__ == "__main__":
    main()
        
