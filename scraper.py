"""
Encar.com car listing scraper.
Uses encar's internal API to fetch structured JSON listing data.
"""

import re
import logging
from urllib.parse import urlparse, parse_qs, unquote

import requests

logger = logging.getLogger(__name__)

# Encar internal API endpoint (returns JSON)
API_BASE_URL = "http://api.encar.com/search/car/list/general"
DETAIL_URL_TEMPLATE = "https://www.encar.com/dc/dc_cardetailview.do?carid={car_id}"


def build_query_from_filters(filters: dict) -> str:
    """
    Build encar's proprietary query string from user-friendly filter dict.

    Encar uses a nested parenthesized query format like:
        (And.Hidden.N._.CarType.Y._.Manufacturer.현대._.Year.range(2020..2024).)
    """
    conditions = ["Hidden.N"]

    car_type = filters.get("car_type", "kor")
    if car_type == "kor":
        conditions.append("CarType.Y")
    else:
        conditions.append("CarType.N")

    if filters.get("manufacturer"):
        conditions.append(f"Manufacturer.{filters['manufacturer']}")

    if filters.get("model"):
        conditions.append(f"ModelGroup.{filters['model']}")

    # Year range
    year_min = filters.get("year_min", "")
    year_max = filters.get("year_max", "")
    if year_min or year_max:
        year_min = year_min or ""
        year_max = year_max or ""
        conditions.append(f"Year.range({year_min}..{year_max})")

    # Price range (in 만원)
    price_min = filters.get("price_min", "")
    price_max = filters.get("price_max", "")
    if price_min or price_max:
        price_min = price_min or ""
        price_max = price_max or ""
        conditions.append(f"Price.range({price_min}..{price_max})")

    # Mileage range
    mileage_min = filters.get("mileage_min", "")
    mileage_max = filters.get("mileage_max", "")
    if mileage_min or mileage_max:
        mileage_min = mileage_min or ""
        mileage_max = mileage_max or ""
        conditions.append(f"Mileage.range({mileage_min}..{mileage_max})")

    # Fuel type
    if filters.get("fuel_type"):
        conditions.append(f"FuelType.{filters['fuel_type']}")

    # Transmission
    if filters.get("transmission"):
        conditions.append(f"Transmission.{filters['transmission']}")

    query = "(And." + "._.".join(conditions) + ".)"
    return query


def extract_query_from_url(url: str) -> str:
    """
    Extract or build the 'q' query parameter from an encar search URL.

    Supports:
        - Desktop URLs with hash fragment: www.encar.com/...#!...q=(And.Hidden.N...)
        - Desktop/API URLs with ?q= parameter
        - Mobile URLs (m.encar.com) with TG.* parameters
    """
    parsed = urlparse(url)

    # Handle the main search page URL with hash-based params
    # e.g., https://www.encar.com/dc/dc_carsearchlist.do?carType=kor#!...
    if parsed.fragment:
        # Fragment-based parameters (after #!)
        fragment = parsed.fragment.lstrip("!")
        # Fragment may be URL-encoded itself
        fragment = unquote(fragment)
        fragment_params = parse_qs(fragment)
        if "q" in fragment_params:
            return unquote(fragment_params["q"][0])

    # Handle direct API URL or desktop URL with q param
    params = parse_qs(parsed.query)
    if "q" in params:
        return unquote(params["q"][0])

    # Handle search condition parameter
    if "searchCondition" in params:
        return unquote(params["searchCondition"][0])

    # Handle mobile URLs (m.encar.com) with TG.* parameters
    # e.g., ?carType=kor&TG.Manufacturer=현대&TG.Model=그랜저&TG.Year_min=2020
    tg_params = {k: v[0] for k, v in params.items() if k.startswith("TG.")}
    if tg_params:
        return _build_query_from_tg_params(params, tg_params)

    # Default: construct a basic query from carType
    car_type = params.get("carType", ["kor"])[0]
    if car_type == "kor":
        return "(And.Hidden.N._.CarType.Y.)"
    else:
        return "(And.Hidden.N._.CarType.N.)"


def _build_query_from_tg_params(params: dict, tg_params: dict) -> str:
    """
    Convert m.encar.com TG.* URL parameters into encar's query format.

    Mobile URL params like:
        TG.Manufacturer=현대, TG.Model=그랜저, TG.Year_min=2020
    become:
        (And.Hidden.N._.CarType.Y._.Manufacturer.현대._.ModelGroup.그랜저._.Year.range(2020..).)
    """
    conditions = ["Hidden.N"]

    # Car type
    car_type = params.get("carType", ["kor"])[0]
    conditions.append("CarType.Y" if car_type == "kor" else "CarType.N")

    # Manufacturer
    mfr = tg_params.get("TG.Manufacturer", "")
    if mfr:
        conditions.append(f"Manufacturer.{mfr}")

    # Model
    model = tg_params.get("TG.Model", "")
    if model:
        conditions.append(f"ModelGroup.{model}")

    # Badge / sub-model
    badge = tg_params.get("TG.Badge", "")
    if badge:
        conditions.append(f"Badge.{badge}")

    badge_detail = tg_params.get("TG.BadgeDetail", "")
    if badge_detail:
        conditions.append(f"BadgeDetail.{badge_detail}")

    # Year range
    year_min = tg_params.get("TG.Year_min", "")
    year_max = tg_params.get("TG.Year_max", "")
    if year_min or year_max:
        conditions.append(f"Year.range({year_min}..{year_max})")

    # Price range (만원)
    price_min = tg_params.get("TG.Price_min", "")
    price_max = tg_params.get("TG.Price_max", "")
    if price_min or price_max:
        conditions.append(f"Price.range({price_min}..{price_max})")

    # Mileage range
    mileage_min = tg_params.get("TG.Mileage_min", "")
    mileage_max = tg_params.get("TG.Mileage_max", "")
    if mileage_min or mileage_max:
        conditions.append(f"Mileage.range({mileage_min}..{mileage_max})")

    # Fuel type
    fuel = tg_params.get("TG.FuelType", "")
    if fuel:
        conditions.append(f"FuelType.{fuel}")

    # Transmission
    trans = tg_params.get("TG.Transmission", "")
    if trans:
        conditions.append(f"Transmission.{trans}")

    # Color
    color = tg_params.get("TG.Color", "")
    if color:
        conditions.append(f"Color.{color}")

    query = "(And." + "._.".join(conditions) + ".)"
    logger.info(f"Built query from mobile URL params: {query}")
    return query


def fetch_listings(query: str, max_results: int = 50) -> list[dict]:
    """
    Fetch car listings from encar API.

    Returns a list of car dicts with keys like:
        Id, Manufacturer, Model, Badge, FormYear, Year, Mileage,
        Price, FuelType, Transmission, Color, etc.
    """
    params = {
        "count": "true",
        "q": query,
        "sr": f"|ModifiedDate|0|{max_results}",
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://www.encar.com/",
    }

    try:
        response = requests.get(
            API_BASE_URL, params=params, headers=headers, timeout=30
        )
        response.raise_for_status()
        data = response.json()

        # The API returns {"Count": N, "SearchResults": [...]}
        listings = data.get("SearchResults", [])
        total = data.get("Count", 0)

        logger.info(f"Fetched {len(listings)} listings (total matching: {total})")
        return listings

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch listings: {e}")
        return []
    except ValueError as e:
        logger.error(f"Failed to parse API response: {e}")
        return []


def parse_listing(raw: dict) -> dict:
    """
    Parse a raw listing from the API into a clean, readable dict.
    """
    car_id = raw.get("Id", "")
    return {
        "id": car_id,
        "url": DETAIL_URL_TEMPLATE.format(car_id=car_id),
        "manufacturer": raw.get("Manufacturer", ""),
        "model": raw.get("Model", ""),
        "badge": raw.get("Badge", ""),
        "badge_detail": raw.get("BadgeDetail", ""),
        "form_year": raw.get("FormYear", ""),
        "year": raw.get("Year", ""),
        "mileage": raw.get("Mileage", 0),
        "price": raw.get("Price", 0),  # in 만원
        "fuel_type": raw.get("FuelType", ""),
        "transmission": raw.get("Transmission", ""),
        "color": raw.get("Color", ""),
        "region": raw.get("OfficeCityState", ""),
        "seller_type": raw.get("SellerType", ""),
        "modified_date": raw.get("ModifiedDate", ""),
        "photo": raw.get("Photo", ""),
    }


def get_car_listings(filters: dict = None, search_url: str = None,
                     max_results: int = 50) -> list[dict]:
    """
    Main entry point: get parsed car listings.

    Either provide `filters` dict or a raw `search_url` from encar.com.
    """
    if search_url:
        query = extract_query_from_url(search_url)
        logger.info(f"Using query extracted from URL: {query}")
    elif filters:
        query = build_query_from_filters(filters)
        logger.info(f"Using query built from filters: {query}")
    else:
        query = "(And.Hidden.N._.CarType.Y.)"
        logger.info("No filters provided, using default query (all Korean cars)")

    raw_listings = fetch_listings(query, max_results)
    return [parse_listing(item) for item in raw_listings]
