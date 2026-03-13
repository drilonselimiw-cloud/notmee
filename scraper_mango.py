"""
MangoCar (mangoworldcar.com) car listing scraper.
Fetches car listings by scraping the server-rendered Next.js page.
Uses cloudscraper to bypass Bunny CDN bot protection.
"""

import re
import json
import logging
from urllib.parse import urlparse, parse_qs, urlencode, quote

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

BASE_URL = "https://mangoworldcar.com/car-normal-search-list"
DETAIL_URL_TEMPLATE = "https://mangoworldcar.com/car-detail/{code}"

# Fuel type code → label
FUEL_TYPES = {
    "0101": "Gasoline",
    "0102": "Diesel",
    "0103": "LPG",
    "0104": "Gasoline+Electric",
    "0105": "Electric",
    "0106": "Hydrogen",
    "0107": "CNG",
}

# Fuel label → code (for building filters)
FUEL_LABEL_TO_CODE = {v.lower(): k for k, v in FUEL_TYPES.items()}
FUEL_LABEL_TO_CODE.update({
    "gas": "0101", "gasoline": "0101", "petrol": "0101",
    "diesel": "0102",
    "lpg": "0103",
    "hybrid": "0104",
    "electric": "0105", "ev": "0105",
    "hydrogen": "0106",
})

# Gearbox code → label
GEARBOX_TYPES = {
    "0101": "Auto",
    "0102": "Manual",
    "0103": "CVT",
    "0104": "Semi-Auto",
}

# Car type labels
CAR_TYPES = {
    "sedan": "0101",
    "mpv": "0102", "van": "0102",
    "suv": "0103",
    "truck": "0201",
    "bus": "0202",
}

# Known manufacturer codes (most popular)
MANUFACTURER_CODES = {
    "hyundai": "Hyundai:0001",
    "genesis": "Genesis:0002",
    "kia": "Kia:0003",
    "renault korea": "Renault Korea (Samsung):0004",
    "samsung": "Renault Korea (Samsung):0004",
    "chevrolet": "Chevrolet:0005",
    "kg mobility": "KG Mobility (Ssangyong):0007",
    "ssangyong": "KG Mobility (Ssangyong):0007",
    "benz": "Benz:0011",
    "mercedes": "Benz:0011",
    "bmw": "BMW:0012",
    "audi": "Audi:0013",
    "volkswagen": "Volkswagen:0014",
    "vw": "Volkswagen:0014",
    "mini": "Mini:0015",
    "land rover": "Land Rover:0017",
    "porsche": "Porsche:0018",
    "jeep": "Jeep:0020",
    "ford": "Ford:0024",
    "volvo": "Volvo:0016",
    "lexus": "Lexus:0023",
    "tesla": "Tesla:0025",
    "toyota": "Toyota:0022",
    "honda": "Honda:0021",
    "peugeot": "Peugeot:0026",
}


# Browser impersonation profiles to rotate through
_IMPERSONATE_PROFILES = ["chrome", "chrome110", "edge99", "safari15_5"]


def build_mango_url(filters: dict) -> str:
    """
    Build a mangoworldcar.com search URL from a user-friendly filter dict.

    Supported filter keys:
        manufacturer, model, car_type, year_min, year_max,
        price_min, price_max, mileage_max, fuel_type
    """
    params = {}

    # Manufacturer (and optional model)
    manufacturer = (filters.get("manufacturer") or "").strip()
    if manufacturer:
        maker_key = manufacturer.lower()
        if maker_key in MANUFACTURER_CODES:
            maker_val = MANUFACTURER_CODES[maker_key]
        else:
            # Try direct format "Brand:Code"
            maker_val = manufacturer
        params["maker"] = maker_val

    # Car type
    car_type = (filters.get("car_type") or "").strip().lower()
    if car_type and car_type in CAR_TYPES:
        params["carType"] = CAR_TYPES[car_type]

    # Year range
    year_min = filters.get("year_min", "")
    year_max = filters.get("year_max", "")
    if year_min:
        params["yearMin"] = str(year_min)
    if year_max:
        params["yearMax"] = str(year_max)

    # Price range (in USD for MangoCar)
    price_min = filters.get("price_min", "")
    price_max = filters.get("price_max", "")
    if price_min:
        params["priceMin"] = str(price_min)
    if price_max:
        params["priceMax"] = str(price_max)

    # Mileage
    mileage_max = filters.get("mileage_max", "")
    if mileage_max:
        params["drivenDistanceMax"] = str(mileage_max)

    # Fuel type
    fuel = (filters.get("fuel_type") or "").strip().lower()
    if fuel and fuel in FUEL_LABEL_TO_CODE:
        params["fuel"] = FUEL_LABEL_TO_CODE[fuel]

    if params:
        query = "&".join(f"{k}={quote(str(v), safe=':,')}" for k, v in params.items())
        return f"{BASE_URL}?{query}"
    return BASE_URL


def _extract_rsc_car_data(html: str) -> list[dict]:
    """
    Extract car listing data from the Next.js RSC (React Server Components)
    streaming data embedded in the page HTML.

    MangoCar uses Apollo GraphQL with SSR data that gets embedded in
    self.__next_f.push() calls.
    """
    all_items = []

    # Strategy 1: Look for Apollo SSR rehydrate data
    for m in re.finditer(
        r'\(window\[Symbol\.for\("ApolloSSRDataTransport"\)\]\s*\?\?=\s*\[\]\)\.push\(',
        html
    ):
        start = m.end()
        # Balance braces to extract JSON
        depth = 0
        in_string = False
        escape_next = False
        end_idx = start

        for i in range(start, min(start + 500000, len(html))):
            c = html[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break

        raw_json = html[start:end_idx]
        # Fix JavaScript undefined → null for valid JSON
        fixed = re.sub(r':undefined([,}])', r':null\1', raw_json)
        try:
            data = json.loads(fixed)
            rehydrate = data.get("rehydrate", {})
            for val in rehydrate.values():
                if isinstance(val, dict) and isinstance(val.get("data"), dict):
                    query_data = val["data"]
                    if "carDetails" in query_data:
                        items = query_data["carDetails"].get("items", [])
                        if items:
                            all_items.extend(items)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # Strategy 2: Parse RSC streaming chunks if Apollo data was empty
    if not all_items:
        for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL):
            content = m.group(1)
            if '"carDataCode"' not in content and "carDetails" not in content:
                continue
            # Find the JSON data portion
            json_start = content.find('[{"type":"data"')
            if json_start < 0:
                json_start = content.find('[{')
            if json_start < 0:
                continue

            # Unescape the JSON string
            raw = content[json_start:]
            try:
                unescaped = raw.encode("utf-8").decode("unicode_escape")
            except (UnicodeDecodeError, ValueError):
                unescaped = raw.replace('\\"', '"').replace('\\\\', '\\')

            # Balance brackets to extract valid JSON
            depth = 0
            in_str = False
            esc = False
            for j, c in enumerate(unescaped):
                if esc:
                    esc = False
                    continue
                if c == '\\' and in_str:
                    esc = True
                    continue
                if c == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c in '{[':
                    depth += 1
                elif c in '}]':
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(unescaped[:j + 1])
                            if isinstance(parsed, list) and parsed:
                                first = parsed[0]
                                if isinstance(first, dict) and "result" in first:
                                    result_data = first["result"].get("data", {})
                                    if "carDetails" in result_data:
                                        items = result_data["carDetails"].get("items", [])
                                        if items:
                                            all_items.extend(items)
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass
                        break

    return all_items


def parse_mango_listing(raw: dict) -> dict:
    """
    Parse a raw MangoCar listing into a clean, normalized dict.
    Output format matches the encar scraper's output where possible.
    """
    code = raw.get("carDataCode", "")
    car_data = raw.get("carData", {}) or {}
    category = raw.get("carCategory", {}) or {}
    category_name = category.get("newCARCategoryName", "")

    # Parse "Hyundai > Tucson" or "Hyundai > Tucson > All New Tucson..."
    parts = [p.strip() for p in category_name.split(">")]
    manufacturer = parts[0] if len(parts) > 0 else ""
    model = parts[1] if len(parts) > 1 else ""
    badge = " > ".join(parts[2:]) if len(parts) > 2 else ""

    fuel_code = car_data.get("fuelType", "")
    gear_code = car_data.get("gearBoxType", "")

    price = raw.get("sellPrice", 0) or 0
    discount_price = raw.get("discountPrice", 0) or 0

    return {
        "id": code,
        "url": DETAIL_URL_TEMPLATE.format(code=code),
        "manufacturer": manufacturer,
        "model": model,
        "badge": badge,
        "badge_detail": car_data.get("gradeName", "") or "",
        "form_year": raw.get("modelYear", ""),
        "year": raw.get("modelYear", ""),
        "mileage": raw.get("driveDistance", 0),
        "price": price,
        "discount_price": discount_price,
        "fuel_type": FUEL_TYPES.get(fuel_code, fuel_code),
        "transmission": GEARBOX_TYPES.get(gear_code, gear_code),
        "displacement": car_data.get("displacement", 0),
        "color": "",
        "region": "Korea",
        "platform": "mango",
    }


def fetch_mango_listings(url: str) -> list[dict]:
    """
    Fetch and parse car listings from a mangoworldcar.com search URL.
    Returns a list of raw car dicts from the page's embedded data.
    Uses curl_cffi with browser impersonation to bypass Bunny CDN.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for profile in _IMPERSONATE_PROFILES:
        try:
            logger.info(f"MangoCar: Trying with impersonate={profile}")
            response = curl_requests.get(
                url, headers=headers, impersonate=profile, timeout=45
            )
            response.raise_for_status()

            if len(response.text) < 5000:
                logger.warning("MangoCar: Short response with %s, trying next", profile)
                continue

            items = _extract_rsc_car_data(response.text)
            logger.info(f"MangoCar: Extracted {len(items)} listings from page")
            return items

        except Exception as e:
            logger.warning(f"MangoCar fetch error with {profile}: {e}")
            continue

    logger.error("MangoCar: All impersonation profiles failed")
    return []


def fetch_first_registration_year(car_code: str) -> int | None:
    """
    Fetch a MangoCar detail page and extract the First Registration year.
    Returns the year as int (e.g. 2016) or None if not found.
    """
    detail_url = DETAIL_URL_TEMPLATE.format(code=car_code)
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for profile in _IMPERSONATE_PROFILES:
        try:
            resp = curl_requests.get(
                detail_url, headers=headers, impersonate=profile, timeout=45
            )
            if resp.status_code != 200:
                continue
            # Look for "First Registration Date</span>...<span...>2016.05.31" or "2016.05"
            m = re.search(
                r'First Registration Date</span>.*?<span[^>]*>.*?(\d{4})\.\d{2}',
                resp.text, re.DOTALL
            )
            if m:
                year = int(m.group(1))
                logger.info(f"MangoCar: {car_code} first reg year = {year}")
                return year
            logger.warning(f"MangoCar: First reg date not found for {car_code}")
            return None
        except Exception as e:
            logger.warning(f"MangoCar detail fetch error ({profile}): {e}")
            continue
    return None


def get_mango_listings(filters: dict = None, search_url: str = None,
                       max_results: int = 50) -> list[dict]:
    """
    Main entry point: get parsed car listings from mangoworldcar.com.

    Either provide `filters` dict or a raw `search_url` from the site.
    """
    if search_url:
        url = search_url
        logger.info(f"MangoCar: Using provided URL: {url}")
    elif filters:
        url = build_mango_url(filters)
        logger.info(f"MangoCar: Built URL from filters: {url}")
    else:
        url = BASE_URL
        logger.info("MangoCar: No filters, using base URL")

    raw_items = fetch_mango_listings(url)
    parsed = [parse_mango_listing(item) for item in raw_items]

    # Limit results
    if len(parsed) > max_results:
        parsed = parsed[:max_results]

    logger.info(f"MangoCar: Returning {len(parsed)} parsed listings")
    return parsed
