"""
Telegram notification module.
Sends formatted messages via Telegram Bot API when new car listings are found.
"""

import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def format_price(price, platform: str = "encar") -> str:
    """Format price. Encar uses 만원, MangoCar uses USD."""
    price = int(price)
    if platform == "mango":
        return f"${price:,}"
    # Encar: 만원 format
    if price >= 10000:
        억 = price // 10000
        만 = price % 10000
        if 만:
            return f"{억}억 {만:,}만원"
        return f"{억}억원"
    return f"{price:,}만원"


def format_mileage(km) -> str:
    """Format mileage to a readable string."""
    return f"{int(km):,} km"


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


def build_car_message(car: dict) -> str:
    """Build a Telegram message for a single car listing."""
    platform = car.get("platform", "encar")

    title = f"{car['manufacturer']} {car['model']}"
    if car.get("badge"):
        title += f" {car['badge']}"
    if car.get("badge_detail"):
        title += f" {car['badge_detail']}"

    lines = [
        f"🚗 *{_escape_md(title)}*",
        "",
        f"📅 Year: {_escape_md(str(car.get('form_year', car.get('year', 'N/A'))))}",
        f"💰 Price: {_escape_md(format_price(car['price'], platform))}",
        f"🛣️ Mileage: {_escape_md(format_mileage(car['mileage']))}",
        f"⛽ Fuel: {_escape_md(car.get('fuel_type', 'N/A'))}",
        f"⚙️ Trans: {_escape_md(car.get('transmission', 'N/A'))}",
    ]

    if car.get("displacement"):
        lines.append(f"🔧 Engine: {_escape_md(str(car['displacement']))}cc")
    if car.get("color"):
        lines.append(f"🎨 Color: {_escape_md(car['color'])}")
    if car.get("region"):
        lines.append(f"📍 Region: {_escape_md(car['region'])}")

    # Platform-specific link label
    if platform == "mango":
        link_label = "View on MangoCar →"
    else:
        link_label = "View on Encar →"
    lines.append("")
    lines.append(f"[{link_label}]({car['url']})")

    return "\n".join(lines)


def send_telegram(
    new_cars: list[dict],
    bot_token: str,
    chat_id: str,
    filter_name: str = "",
    platform: str = "encar",
) -> bool:
    """
    Send a single Telegram message listing all new car URLs.
    Only called when there are new cars (newCars > 0).
    Returns True if message sent successfully, False otherwise.
    """
    if not new_cars:
        return False

    url = TELEGRAM_API_URL.format(token=bot_token)

    # Build header based on platform
    if platform == "mango":
        platform_label = "Mango World Car"
    else:
        platform_label = "Encar"

    count = len(new_cars)
    filter_label = f" [{filter_name}]" if filter_name else ""

    lines = [f"🔔 List of new cars added in {platform_label}{filter_label}:"]
    lines.append("")

    for car in new_cars:
        lines.append(car["url"])

    lines.append("")
    lines.append(f"Total: {count} new car{'s' if count != 1 else ''}")

    text = "\n".join(lines)

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            logger.info(f"Telegram notification sent: {count} new car(s) for '{filter_name}'")
            return True
        else:
            logger.error(f"Telegram API error {resp.status_code}: {resp.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def send_telegram_text(text: str, bot_token: str, chat_id: str,
                       parse_mode: str = None) -> bool:
    """Send a plain text message via Telegram."""
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code == 200
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Telegram text: {e}")
        return False
