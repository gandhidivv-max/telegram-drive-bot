import os
import json
import requests
from datetime import datetime
import pytz

# Secrets from GitHub Environment
PINTEREST_APP_ID = os.getenv("PINTEREST_APP_ID")
PINTEREST_APP_SECRET = os.getenv("PINTEREST_APP_SECRET")
PINTEREST_REFRESH_TOKEN = os.getenv("PINTEREST_REFRESH_TOKEN")

# Channel Telegram Details
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Personal Admin Telegram Details
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

STATE_FILE = "state.json"
POSTED_PINS_FILE = "posted_pins.json"

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            return default
    return default

def save_json(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
        print(f"Successfully saved {filepath}")
    except Exception as e:
        print(f"Error saving {filepath}: {e}")

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
        items = res.json().get("items", [])
        return [{"id": board["id"], "name": board.get("name", "Pinterest")} for board in items]
    print(f"Failed to fetch boards: {res.text}")
    return []

def get_pins_from_board(access_token, board_id):
    url = f"https://api.pinterest.com/v5/boards/{board_id}/pins"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = requests.get(url, headers=headers)
    if res.ok:
        return res.json().get("items", [])
    return []

def send_telegram_photo(image_url, caption, board_name):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    
    # Channel లో మాత్రమే బటన్ కనిపిస్తుంది (Group లో కట్ అవుతుంది)
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
        "photo": image_url,
        "caption": caption,
        "reply_markup": json.dumps(reply_markup)
    }
    res = requests.post(url, json=payload)
    if not res.ok:
        print(f"Failed to send photo to Telegram: {res.text}")
    return res.ok

def send_admin_report(report_text):
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        print("Admin Bot secrets missing. Skipping report.")
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
    current_start_index = state.get("board_index", 0) % total_boards
    
    print(f"Total Boards: {total_boards} | Starting Board Index: {current_start_index}")

    posted_successfully = False
    
    # ప్రతీ బోర్డును ఒకదాని తర్వాత ఒకటి చెక్ చేసే లాజిక్
    for i in range(total_boards):
        eval_index = (current_start_index + i) % total_boards
        board_info = boards[eval_index]
        board_id = board_info["id"]
        board_name = board_info["name"]

        pins = get_pins_from_board(access_token, board_id)

        for pin in pins:
            pin_id = pin.get("id")
            # పాత పిన్ అయితే స్కిప్ చేస్తుంది
            if pin_id in posted_pins:
                continue

            media = pin.get("media", {})
            images = media.get("images", {})
            image_url = images.get("originals", {}).get("url") or images.get("600x", {}).get("url")

            if image_url:
                caption = pin.get("title") or pin.get("description") or ""
                
                if send_telegram_photo(image_url, caption, board_name):
                    print(f"✅ SUCCESS: Posted Pin {pin_id} from Board: {board_name} (Index {eval_index})")
                    posted_pins.add(pin_id)
                    
                    # తదుపరి రన్ కోసం బోర్డు ఇండెక్స్‌ను కచ్చితంగా ముందుకు జరుపుతుంది
                    next_board_index = (eval_index + 1) % total_boards
                    state["board_index"] = next_board_index
                    print(f"🔄 Next run will start at Board Index: {next_board_index}")
                    
                    posted_successfully = True
                    break

        if posted_successfully:
            break

    if not posted_successfully:
        print("No new pins found across any boards.")

    # State & Posted Pins కచ్చితంగా ఫైల్స్‌కి సేవ్ చేయడం
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
            f"🚀 Bot Status: Working smoothly!"
        )
        send_admin_report(report_msg)
        state["last_report_date"] = today_str
        save_json(STATE_FILE, state)

if __name__ == "__main__":
    main()
                      
