"""
Car Monitor — Interactive Telegram Bot (Multi-platform).

Monitors encar.com and mangoworldcar.com for new listings
across multiple filters simultaneously.

Commands (send in Telegram chat):
    /start          — Welcome & help
    /add <name>     — Add a new filter (guided: choose platform + params)
    /url <name> <url> — Quick-add from an encar or mangoworldcar URL
    /list           — List all filters
    /remove <id>    — Remove a filter
    /pause <id>     — Pause a filter
    /resume <id>    — Resume a filter
    /clear <id>     — Reset seen cars for a filter
    /status         — Show monitor status
    /help           — Show commands
"""

import os
import logging
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from scraper import get_car_listings, build_query_from_filters
from scraper_mango import get_mango_listings, fetch_first_registration_year
from notifier import send_telegram, format_price, format_mileage
from filter_store import (
    add_filter, remove_filter, pause_filter, resume_filter,
    get_all_filters, get_active_filters, get_filter,
    get_seen_ids, get_seen_cars, update_seen_ids, clear_seen_ids,
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "encar_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "50"))

# Track the chat_id from whoever messages the bot
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------------------
# Telegram Bot (polling-based, no external lib needed)
# ---------------------------------------------------------------------------
import requests as http_requests

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET = 0  # Track last processed update


def tg_send(chat_id: str, text: str):
    """Send a plain-text message."""
    http_requests.post(f"{API}/sendMessage", json={
        "chat_id": chat_id, "text": text,
    }, timeout=15)


def tg_get_updates(offset: int, timeout: int = 30) -> list:
    """Long-poll for new messages."""
    try:
        resp = http_requests.get(f"{API}/getUpdates", params={
            "offset": offset, "timeout": timeout,
        }, timeout=timeout + 5)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        logger.error(f"Polling error: {e}")
    return []


# ---------------------------------------------------------------------------
# Conversation state for multi-step /add
# ---------------------------------------------------------------------------
# chat_id -> {"step": str, "name": str, "params": dict}
CONVERSATIONS = {}

ADD_STEPS = [
    ("car_type", "🚗 Car type?\n\nType: kor (Korean) or for (imported)\n\nOr send 'skip' for default (kor)"),
    ("manufacturer", "🏭 Manufacturer?\n\nExamples: 현대, 기아, 제네시스, BMW, 벤츠, 테슬라\n\nOr send 'skip' for any"),
    ("model", "📋 Model?\n\nExamples: 그랜저, 쏘나타, K5, 팰리세이드\n\nOr send 'skip' for any"),
    ("year_min", "📅 Minimum year? (e.g., 2020)\n\nOr send 'skip' for no limit"),
    ("year_max", "📅 Maximum year? (e.g., 2025)\n\nOr send 'skip' for no limit"),
    ("price_min", "💰 Minimum price in 만원? (e.g., 1000 = ~$7.5k)\n\nOr send 'skip' for no limit"),
    ("price_max", "💰 Maximum price in 만원? (e.g., 3000 = ~$22k)\n\nOr send 'skip' for no limit"),
    ("mileage_max", "🛣️ Maximum mileage in km? (e.g., 50000)\n\nOr send 'skip' for no limit"),
    ("fuel_type", "⛽ Fuel type?\n\nOptions: 가솔린, 디젤, LPG, 가솔린+전기, 전기, 수소\n\nOr send 'skip' for any"),
]


def format_filter_summary(f: dict) -> str:
    """Format a filter dict into a readable summary string."""
    p = f.get("params", {})
    platform = f.get("platform", "encar")
    platform_label = "🇰🇷 Encar" if platform == "encar" else "🍊 MangoCar"
    status = "✅ Active" if f.get("active") else "⏸️ Paused"
    lines = [f"[{f['id']}] {f['name']} — {status}", f"  {platform_label}"]

    if p.get("search_url"):
        lines.append(f"  🔗 URL filter")
        if p.get("min_year"):
            lines.append(f"  📅 Min first reg year: {p['min_year']}")
    else:
        parts = []
        if p.get("manufacturer"):
            parts.append(p["manufacturer"])
        if p.get("model"):
            parts.append(p["model"])
        if p.get("year_min") or p.get("year_max"):
            parts.append(f"{p.get('year_min', '?')}~{p.get('year_max', '?')}년")
        if p.get("price_min") or p.get("price_max"):
            price_unit = "USD" if platform == "mango" else "만원"
            parts.append(f"{p.get('price_min', '?')}~{p.get('price_max', '?')} {price_unit}")
        if p.get("mileage_max"):
            parts.append(f"≤{p['mileage_max']}km")
        if p.get("fuel_type"):
            parts.append(p["fuel_type"])
        if parts:
            lines.append(f"  {' | '.join(parts)}")

    seen_count = len(f.get("seen_ids", []))
    total = f.get("total_found", 0)
    lines.append(f"  📊 {total} new found | {seen_count} tracked")
    if f.get("last_checked"):
        lines.append(f"  🕐 Last: {f['last_checked'][:16]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def handle_message(chat_id: str, text: str):
    """Route incoming message to the right handler."""
    global CHAT_ID

    # Remember chat_id
    if not CHAT_ID:
        CHAT_ID = str(chat_id)

    text = text.strip()

    # Check if we're in a conversation
    if str(chat_id) in CONVERSATIONS:
        handle_conversation_step(chat_id, text)
        return

    if text.startswith("/"):
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower().split("@")[0]  # strip @botname
        args = parts[1:] if len(parts) > 1 else []

        if cmd == "/start" or cmd == "/help":
            cmd_help(chat_id)
        elif cmd == "/add":
            cmd_add(chat_id, args)
        elif cmd == "/url":
            cmd_url(chat_id, args, text)
        elif cmd == "/list":
            cmd_list(chat_id)
        elif cmd == "/remove":
            cmd_remove(chat_id, args)
        elif cmd == "/pause":
            cmd_pause(chat_id, args)
        elif cmd == "/resume":
            cmd_resume(chat_id, args)
        elif cmd == "/clear":
            cmd_clear(chat_id, args)
        elif cmd == "/get":
            cmd_get(chat_id, args)
        elif cmd == "/status":
            cmd_status(chat_id)
        else:
            tg_send(chat_id, f"Unknown command: {cmd}\nSend /help for available commands.")
    else:
        tg_send(chat_id, "Send /help to see available commands.")


def cmd_help(chat_id):
    tg_send(chat_id, (
        "🚗 *Car Monitor* (Encar + MangoCar) — Commands:\n\n"
        "/add <name> — Add filter (choose platform + params)\n"
        "/url <name> <url> — Quick-add from encar or mangoworldcar URL\n"
        "/list — Show all filters\n"
        "/remove <id or name> — Delete a filter\n"
        "/pause <id or name> — Pause monitoring\n"
        "/resume <id or name> — Resume monitoring\n"
        "/clear <id or name> — Reset seen cars\n"
        "/get <id or name> — List matched cars\n"
        "/status — Monitor status\n"
        "/help — This message\n\n"
        f"⏱️ Checking every {CHECK_INTERVAL} min\n"
        "🌐 Platforms: encar.com | mangoworldcar.com"
    ))


def cmd_add(chat_id, args):
    if not args:
        tg_send(chat_id, "Usage: /add <filter_name>\n\nExample: /add Tucson SUV")
        return
    name = " ".join(args)
    CONVERSATIONS[str(chat_id)] = {
        "step": -1,  # -1 = platform selection step
        "name": name,
        "platform": None,
        "params": {},
    }
    _, prompt = PLATFORM_STEP
    tg_send(chat_id, f"Creating filter: \"{name}\"\n\n{prompt}")


def handle_conversation_step(chat_id, text):
    chat_id = str(chat_id)
    conv = CONVERSATIONS[chat_id]

    if text.lower() == "/cancel":
        del CONVERSATIONS[chat_id]
        tg_send(chat_id, "❌ Filter creation cancelled.")
        return

    step_idx = conv["step"]

    # Step -1: Platform selection
    if step_idx == -1:
        choice = text.strip().lower()
        if choice in ("1", "encar"):
            conv["platform"] = "encar"
        elif choice in ("2", "mango", "mangoworldcar", "mangocar"):
            conv["platform"] = "mango"
        else:
            tg_send(chat_id, "⚠️ Please type 'encar' or 'mango'")
            return
        conv["step"] = 0
        steps = ADD_STEPS_ENCAR if conv["platform"] == "encar" else ADD_STEPS_MANGO
        _, prompt = steps[0]
        platform_name = "encar.com" if conv["platform"] == "encar" else "mangoworldcar.com"
        tg_send(chat_id, f"✅ Platform: {platform_name}\n\n{prompt}")
        return

    # Normal filter parameter steps
    steps = ADD_STEPS_ENCAR if conv["platform"] == "encar" else ADD_STEPS_MANGO
    key, _ = steps[step_idx]

    # Save answer (skip = empty)
    value = "" if text.lower() == "skip" else text.strip()
    conv["params"][key] = value

    # Move to next step
    step_idx += 1
    conv["step"] = step_idx

    if step_idx < len(steps):
        _, prompt = steps[step_idx]
        tg_send(chat_id, prompt)
    else:
        # Done — create the filter
        result = add_filter(conv["name"], conv["params"], platform=conv["platform"])
        del CONVERSATIONS[chat_id]
        if isinstance(result, str):
            tg_send(chat_id, f"❌ {result}")
        else:
            tg_send(chat_id, (
                f"✅ Filter created!\n\n"
                f"{format_filter_summary(result)}\n\n"
                f"Monitoring will start on the next check cycle."
            ))


def cmd_url(chat_id, args, full_text):
    # /url <name> <url> [min_year=XXXX]
    parts = full_text.split(maxsplit=2)
    if len(parts) < 3:
        tg_send(chat_id, (
            "Usage: /url <name> <url> [min_year=XXXX]\n\n"
            "Examples:\n"
            "/url Palisade https://www.encar.com/dc/dc_carsearchlist.do?carType=kor...\n"
            "/url Tucson https://mangoworldcar.com/car-normal-search-list?maker=Hyundai...\n"
            "/url AudiA7 https://mangoworldcar.com/... min_year=2016"
        ))
        return

    name = parts[1]
    rest = parts[2].strip()

    # Extract optional min_year=XXXX from the end
    min_year = None
    rest_parts = rest.split()
    url_parts = []
    for rp in rest_parts:
        m = re.match(r'^min_year=(\d{4})$', rp)
        if m:
            min_year = int(m.group(1))
        else:
            url_parts.append(rp)
    url = " ".join(url_parts)

    # Auto-detect platform from URL
    if "mangoworldcar.com" in url or "mangocar" in url.lower():
        platform = "mango"
    elif "encar.com" in url or "encar" in url.lower():
        platform = "encar"
    else:
        tg_send(chat_id, (
            "⚠️ Unrecognized URL. Supported sites:\n"
            "  • encar.com\n"
            "  • mangoworldcar.com"
        ))
        return

    filter_params = {"search_url": url}
    if min_year and platform == "mango":
        filter_params["min_year"] = min_year

    new_filter = add_filter(name, filter_params, platform=platform)
    if isinstance(new_filter, str):
        tg_send(chat_id, f"❌ {new_filter}")
        return

    # Immediately fetch current listings and store IDs as seen
    # so only truly NEW cars trigger notifications on the next check
    try:
        if platform == "mango":
            listings = get_mango_listings(search_url=url, max_results=MAX_RESULTS)
        else:
            listings = get_car_listings(search_url=url, max_results=MAX_RESULTS)
        if listings:
            if min_year and platform == "mango":
                # Check each car's first registration year
                seen_dict = {}
                for car in listings:
                    reg_year = fetch_first_registration_year(car["id"])
                    if reg_year is None:
                        reg_year = int(car.get("year") or 0)
                    seen_dict[str(car["id"])] = reg_year >= min_year
            else:
                seen_dict = {str(car["id"]): True for car in listings}
            update_seen_ids(new_filter["id"], seen_dict)
            matched = sum(1 for v in seen_dict.values() if v)
            count = len(listings)
        else:
            count = 0
            matched = 0
    except Exception as e:
        logger.warning(f"Initial fetch for '{name}' failed: {e}")
        count = 0
        matched = 0

    msg = f"✅ Filter created from URL!\n\n{format_filter_summary(new_filter)}\n\n"
    msg += f"Stored {count} existing car(s) — you'll only be notified about new ones."
    if min_year and platform == "mango" and count > 0:
        msg += f"\n📅 min_year={min_year}: {matched} matched, {count - matched} filtered out."
    tg_send(chat_id, msg)


def cmd_list(chat_id):
    filters = get_all_filters()
    if not filters:
        tg_send(chat_id, "📭 No filters yet.\n\nUse /add or /url to create one.")
        return

    lines = [f"📋 Your filters ({len(filters)}):\n"]
    for f in filters:
        lines.append(format_filter_summary(f))
        lines.append("")
    tg_send(chat_id, "\n".join(lines))


def cmd_remove(chat_id, args):
    if not args:
        tg_send(chat_id, "Usage: /remove <id or name>\n\nUse /list to see filters.")
        return
    key = " ".join(args)
    name = remove_filter(key)
    if name:
        tg_send(chat_id, f"🗑️ Filter '{name}' removed.")
    else:
        tg_send(chat_id, f"❌ Filter '{key}' not found. Use /list to see filters.")


def cmd_pause(chat_id, args):
    if not args:
        tg_send(chat_id, "Usage: /pause <id or name>")
        return
    key = " ".join(args)
    name = pause_filter(key)
    if name:
        tg_send(chat_id, f"⏸️ Filter '{name}' paused.")
    else:
        tg_send(chat_id, f"❌ Filter '{key}' not found.")


def cmd_resume(chat_id, args):
    if not args:
        tg_send(chat_id, "Usage: /resume <id or name>")
        return
    key = " ".join(args)
    name = resume_filter(key)
    if name:
        tg_send(chat_id, f"▶️ Filter '{name}' resumed.")
    else:
        tg_send(chat_id, f"❌ Filter '{key}' not found.")


def cmd_clear(chat_id, args):
    if not args:
        tg_send(chat_id, "Usage: /clear <id or name>")
        return
    key = " ".join(args)
    name = clear_seen_ids(key)
    if name:
        tg_send(chat_id, f"🔄 Seen cars cleared for '{name}'. It will re-alert on next check.")
    else:
        tg_send(chat_id, f"❌ Filter '{key}' not found.")


def cmd_get(chat_id, args):
    if not args:
        tg_send(chat_id, "Usage: /get <id or name>")
        return
    key = " ".join(args)
    f = get_filter(key)
    if not f:
        tg_send(chat_id, f"❌ Filter '{key}' not found.")
        return

    seen = get_seen_cars(f["id"])
    if not seen:
        tg_send(chat_id, f"📭 No cars for '{f['name']}' yet.")
        return

    platform = f.get("platform", "encar")
    matched = [cid for cid, m in seen.items() if m]

    if not matched:
        tg_send(chat_id, f"📭 No matched cars for '{f['name']}' yet.")
        return

    lines = [f"✅ Matched cars for '{f['name']}' ({len(matched)}):"]
    for cid in matched[:50]:
        if platform == "mango":
            lines.append(f"https://mangoworldcar.com/car-detail/{cid}")
        else:
            lines.append(f"https://www.encar.com/dc/dc_cardetailview.do?carid={cid}")
    if len(matched) > 50:
        lines.append(f"... and {len(matched) - 50} more")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n... (truncated)"
    tg_send(chat_id, msg)


def cmd_status(chat_id):
    active = get_active_filters()
    total = get_all_filters()
    tg_send(chat_id, (
        f"📊 Monitor Status\n\n"
        f"Total filters: {len(total)}\n"
        f"Active: {len(active)}\n"
        f"Paused: {len(total) - len(active)}\n"
        f"Check interval: {CHECK_INTERVAL} min\n"
        f"Max results/check: {MAX_RESULTS}"
    ))


# ---------------------------------------------------------------------------
# Background monitor — checks all active filters periodically
# ---------------------------------------------------------------------------
def monitor_loop():
    """Background thread: periodically check all active filters for new cars."""
    logger.info(f"Monitor loop started — checking every {CHECK_INTERVAL} min")

    while True:
        try:
            active_filters = get_active_filters()
            if not active_filters:
                logger.info("No active filters — skipping check")
            else:
                logger.info(f"Checking {len(active_filters)} active filter(s)...")

                for f in active_filters:
                    try:
                        check_filter(f)
                    except Exception as e:
                        logger.error(f"Error checking filter {f['id']}: {e}")

        except Exception as e:
            logger.error(f"Monitor loop error: {e}")

        time.sleep(CHECK_INTERVAL * 60)


def check_filter(f: dict):
    """Check a single filter for new listings and notify if found."""
    filter_id = f["id"]
    filter_name = f["name"]
    platform = f.get("platform", "encar")
    params = f.get("params", {})

    logger.info(f"Checking filter '{filter_name}' ({filter_id}) on {platform}...")

    # Fetch listings — route to the correct scraper
    try:
        if platform == "mango":
            if params.get("search_url"):
                listings = get_mango_listings(search_url=params["search_url"], max_results=MAX_RESULTS)
            else:
                listings = get_mango_listings(filters=params, max_results=MAX_RESULTS)
        else:  # encar (default)
            if params.get("search_url"):
                listings = get_car_listings(search_url=params["search_url"], max_results=MAX_RESULTS)
            else:
                listings = get_car_listings(filters=params, max_results=MAX_RESULTS)
    except Exception as e:
        logger.error(f"  Scraper error for '{filter_name}': {e}")
        return

    if not listings:
        logger.info(f"  No listings returned for '{filter_name}'")
        return

    # Compare with seen
    seen_ids = get_seen_ids(filter_id)
    new_cars = [car for car in listings if str(car["id"]) not in seen_ids]

    # Apply min_year filter for mango: check first registration year on detail page
    min_year = params.get("min_year")
    seen_update = {}  # {car_id: matched_bool}

    if new_cars and platform == "mango" and min_year:
        min_year_val = int(min_year)
        filtered = []
        for car in new_cars:
            reg_year = fetch_first_registration_year(car["id"])
            if reg_year is None:
                reg_year = int(car.get("year") or 0)
            if reg_year >= min_year_val:
                filtered.append(car)
                seen_update[str(car["id"])] = True
            else:
                seen_update[str(car["id"])] = False
                logger.info(f"  Filtered out {car['id']}: reg year {reg_year} < {min_year_val}")
        logger.info(f"  min_year filter: {len(new_cars)} -> {len(filtered)} cars")
        new_cars = filtered
    else:
        for car in new_cars:
            seen_update[str(car["id"])] = True

    # Also mark already-seen listings (not new) that aren't in seen_update yet
    for car in listings:
        cid = str(car["id"])
        if cid not in seen_update and cid not in seen_ids:
            seen_update[cid] = True
        elif cid not in seen_update:
            pass  # already tracked

    # Update seen IDs with matched info
    # For cars already in seen that aren't new, keep their existing status
    all_old_ids = {str(car["id"]): True for car in listings if str(car["id"]) in seen_ids}
    all_old_ids.update(seen_update)
    update_seen_ids(filter_id, all_old_ids, total_new=len(new_cars))

    if new_cars:
        logger.info(f"  Found {len(new_cars)} new car(s) for '{filter_name}'!")
        for car in new_cars:
            title = f"{car['manufacturer']} {car['model']} {car.get('badge', '')}".strip()
            logger.info(f"    NEW: {title} | {format_price(car['price'], platform)} | {car['url']}")

        # Send notifications
        if CHAT_ID and BOT_TOKEN:
            send_telegram(
                new_cars,
                bot_token=BOT_TOKEN,
                chat_id=CHAT_ID,
                filter_name=filter_name,
                platform=platform,
            )
    else:
        logger.info(f"  No new cars for '{filter_name}' ({len(listings)} checked)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global OFFSET, CHAT_ID

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        print("1. Message @BotFather on Telegram → /newbot")
        print("2. Copy the token to .env")
        return

    print(f"🚗 Car Monitor Bot starting (Encar + MangoCar)...")
    print(f"   Check interval: {CHECK_INTERVAL} min")
    print(f"   Send /start to your bot on Telegram to begin!\n")

    # Start background monitor thread
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    # Main thread: poll for Telegram messages
    logger.info("Bot polling started...")

    while True:
        updates = tg_get_updates(OFFSET)
        for update in updates:
            OFFSET = update["update_id"] + 1

            msg = update.get("message")
            if not msg or not msg.get("text"):
                continue

            chat_id = msg["chat"]["id"]
            text = msg["text"]

            # Auto-capture chat_id
            if not CHAT_ID:
                CHAT_ID = str(chat_id)
                logger.info(f"Chat ID captured: {CHAT_ID}")

            try:
                handle_message(chat_id, text)
            except Exception as e:
                logger.error(f"Error handling message: {e}")
                tg_send(chat_id, f"⚠️ Error: {e}")


if __name__ == "__main__":
    main()
