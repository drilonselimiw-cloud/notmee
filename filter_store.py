"""
Filter storage module.
Manages multiple named car search filters, persisted to Upstash Redis.
Falls back to local JSON file if Redis is not configured.
Each filter has: id, name, search params, active flag, and its own seen-car set.
"""

import json
import os
import uuid
import logging
from datetime import datetime
from pathlib import Path

import requests as http_requests

logger = logging.getLogger(__name__)

# Redis config (Upstash REST API)
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY = "car_bot:filters"

# Local fallback
_local_file = Path(__file__).resolve().parent / "filters.json"


def _redis_available() -> bool:
    return bool(REDIS_URL and REDIS_TOKEN)


def _redis_get() -> dict | None:
    """GET the filters JSON from Upstash Redis."""
    try:
        r = http_requests.get(
            f"{REDIS_URL}/get/{REDIS_KEY}",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result")
        if result:
            return json.loads(result)
    except Exception as e:
        logger.warning(f"Redis GET failed: {e}")
    return None


def _redis_set(data: dict) -> bool:
    """SET the filters JSON in Upstash Redis."""
    try:
        payload = json.dumps(data, ensure_ascii=False)
        r = http_requests.post(
            f"{REDIS_URL}",
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=["SET", REDIS_KEY, payload],
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Redis SET failed: {e}")
        return False


def _load_data() -> dict:
    """Load filters from Redis, falling back to local file."""
    if _redis_available():
        data = _redis_get()
        if data:
            return data
    # Fallback to local file
    if _local_file.exists():
        try:
            return json.loads(_local_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupted filters.json — starting fresh")
    return {"filters": {}}


def _save_data(data: dict) -> None:
    """Persist data to Redis and local file."""
    if _redis_available():
        _redis_set(data)
    # Always save locally too as backup
    _local_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _resolve_filter_id(data: dict, id_or_name: str) -> str | None:
    """
    Resolve a filter by ID or by name (case-insensitive).
    Returns the filter ID if found, None otherwise.
    """
    # Try direct ID match first
    if id_or_name in data["filters"]:
        return id_or_name
    # Try name match (case-insensitive)
    for fid, f in data["filters"].items():
        if f["name"].lower() == id_or_name.lower():
            return fid
    return None


def name_exists(name: str) -> bool:
    """Check if a filter with this name already exists (case-insensitive)."""
    data = _load_data()
    return any(
        f["name"].lower() == name.lower()
        for f in data["filters"].values()
    )


def add_filter(name: str, params: dict, platform: str = "encar") -> dict | str:
    """
    Add a new filter and return it.
    Returns an error string if the name is already taken.

    platform: "encar" or "mango"

    params can contain:
        car_type, manufacturer, model, year_min, year_max,
        price_min, price_max, mileage_min, mileage_max,
        fuel_type, transmission, search_url
    """
    data = _load_data()

    # Enforce unique name
    for f in data["filters"].values():
        if f["name"].lower() == name.lower():
            return f"A filter named '{f['name']}' already exists. Choose a different name."

    filter_id = str(uuid.uuid4())[:8]
    new_filter = {
        "id": filter_id,
        "name": name,
        "platform": platform,
        "params": params,
        "active": True,
        "seen_ids": [],
        "created_at": datetime.now().isoformat(),
        "last_checked": None,
        "total_found": 0,
    }
    data["filters"][filter_id] = new_filter
    _save_data(data)
    logger.info(f"Added filter '{name}' ({filter_id})")
    return new_filter


def remove_filter(id_or_name: str) -> str | None:
    """Remove a filter by ID or name. Returns the filter name if removed, None if not found."""
    data = _load_data()
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        name = data["filters"][fid]["name"]
        del data["filters"][fid]
        _save_data(data)
        logger.info(f"Removed filter '{name}' ({fid})")
        return name
    return None


def pause_filter(id_or_name: str) -> str | None:
    """Pause a filter by ID or name. Returns the filter name if paused, None if not found."""
    data = _load_data()
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        data["filters"][fid]["active"] = False
        _save_data(data)
        return data["filters"][fid]["name"]
    return None


def resume_filter(id_or_name: str) -> str | None:
    """Resume a paused filter by ID or name. Returns the filter name if resumed, None if not found."""
    data = _load_data()
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        data["filters"][fid]["active"] = True
        _save_data(data)
        return data["filters"][fid]["name"]
    return None


def get_all_filters() -> list[dict]:
    """Return all filters (active and paused)."""
    data = _load_data()
    return list(data["filters"].values())


def get_active_filters() -> list[dict]:
    """Return only active filters."""
    return [f for f in get_all_filters() if f["active"]]


def get_filter(id_or_name: str) -> dict | None:
    """Get a single filter by ID or name."""
    data = _load_data()
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        return data["filters"][fid]
    return None


def get_seen_ids(filter_id: str) -> set:
    """Get the set of seen car IDs for a specific filter."""
    data = _load_data()
    f = data["filters"].get(filter_id)
    if f:
        return set(str(x) for x in f.get("seen_ids", []))
    return set()


def update_seen_ids(filter_id: str, new_ids: set, total_new: int = 0) -> None:
    """Add new seen IDs for a filter and update metadata."""
    data = _load_data()
    if filter_id in data["filters"]:
        existing = set(str(x) for x in data["filters"][filter_id].get("seen_ids", []))
        existing.update(str(x) for x in new_ids)
        data["filters"][filter_id]["seen_ids"] = list(existing)
        data["filters"][filter_id]["last_checked"] = datetime.now().isoformat()
        data["filters"][filter_id]["total_found"] = (
            data["filters"][filter_id].get("total_found", 0) + total_new
        )
        _save_data(data)


def clear_seen_ids(id_or_name: str) -> str | None:
    """Clear seen IDs for a filter by ID or name. Returns filter name if cleared, None if not found."""
    data = _load_data()
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        data["filters"][fid]["seen_ids"] = []
        _save_data(data)
        return data["filters"][fid]["name"]
    return None
