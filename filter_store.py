"""
Filter storage module — multi-user.
Each Telegram user (chat_id) has their own set of filters.
Persisted to Upstash Redis with per-user keys: car_bot:filters:{chat_id}
Falls back to local JSON file if Redis is not configured.
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
OLD_REDIS_KEY = "car_bot:filters"  # legacy single-user key
REDIS_KEY_PREFIX = "car_bot:filters:"  # per-user: car_bot:filters:{chat_id}
USERS_KEY = "car_bot:users"  # set of known chat_ids

# Local fallback
_local_dir = Path(__file__).resolve().parent
_old_local_file = _local_dir / "filters.json"


def _redis_available() -> bool:
    return bool(REDIS_URL and REDIS_TOKEN)


def _redis_get_key(key: str) -> dict | None:
    """GET a JSON value from Redis by key."""
    try:
        r = http_requests.get(
            f"{REDIS_URL}/get/{key}",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result")
        if result:
            return json.loads(result)
    except Exception as e:
        logger.warning(f"Redis GET {key} failed: {e}")
    return None


def _redis_set_key(key: str, data: dict) -> bool:
    """SET a JSON value in Redis."""
    try:
        payload = json.dumps(data, ensure_ascii=False)
        r = http_requests.post(
            f"{REDIS_URL}",
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=["SET", key, payload],
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Redis SET {key} failed: {e}")
        return False


def _redis_del_key(key: str) -> bool:
    """DEL a key from Redis."""
    try:
        r = http_requests.post(
            f"{REDIS_URL}",
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=["DEL", key],
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Redis DEL {key} failed: {e}")
        return False


def _register_user(chat_id: str) -> None:
    """Add chat_id to the known users set in Redis."""
    if not _redis_available():
        return
    try:
        http_requests.post(
            f"{REDIS_URL}",
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=["SADD", USERS_KEY, str(chat_id)],
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Redis SADD users failed: {e}")


def get_all_user_ids() -> list[str]:
    """Get all registered user chat_ids from Redis."""
    if not _redis_available():
        # Fallback: scan local files
        ids = []
        for f in _local_dir.glob("filters_*.json"):
            cid = f.stem.replace("filters_", "")
            if cid:
                ids.append(cid)
        return ids
    try:
        r = http_requests.post(
            f"{REDIS_URL}",
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=["SMEMBERS", USERS_KEY],
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result", [])
        return [str(x) for x in result] if result else []
    except Exception as e:
        logger.warning(f"Redis SMEMBERS users failed: {e}")
        return []


def _user_key(chat_id: str) -> str:
    return f"{REDIS_KEY_PREFIX}{chat_id}"


def _local_file(chat_id: str) -> Path:
    return _local_dir / f"filters_{chat_id}.json"


def _migrate_old_data(chat_id: str) -> bool:
    """Migrate old single-user data to the new per-user format. Returns True if migrated."""
    migrated = False

    # Check Redis for old key
    if _redis_available():
        old_data = _redis_get_key(OLD_REDIS_KEY)
        if old_data and old_data.get("filters"):
            logger.info(f"Migrating {len(old_data['filters'])} filters from old key to user {chat_id}")
            _redis_set_key(_user_key(chat_id), old_data)
            _redis_del_key(OLD_REDIS_KEY)
            _register_user(chat_id)
            migrated = True

    # Check local old file
    if _old_local_file.exists():
        try:
            old_data = json.loads(_old_local_file.read_text(encoding="utf-8"))
            if old_data.get("filters"):
                new_file = _local_file(chat_id)
                if not new_file.exists():
                    new_file.write_text(
                        json.dumps(old_data, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                _old_local_file.rename(_old_local_file.with_suffix(".json.bak"))
                logger.info(f"Migrated local filters.json to filters_{chat_id}.json")
                migrated = True
        except Exception as e:
            logger.warning(f"Local migration failed: {e}")

    return migrated


def _load_data(chat_id: str) -> dict:
    """Load filters for a specific user."""
    cid = str(chat_id)

    if _redis_available():
        data = _redis_get_key(_user_key(cid))
        if data:
            return data

    # Fallback to local file
    lf = _local_file(cid)
    if lf.exists():
        try:
            return json.loads(lf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            logger.warning(f"Corrupted {lf.name} — starting fresh")

    # Try migration from old format
    if _migrate_old_data(cid):
        return _load_data(cid)

    return {"filters": {}}


def _save_data(chat_id: str, data: dict) -> None:
    """Persist data for a specific user."""
    cid = str(chat_id)
    if _redis_available():
        _redis_set_key(_user_key(cid), data)
        _register_user(cid)
    # Always save locally too as backup
    _local_file(cid).write_text(
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


def name_exists(chat_id: str, name: str) -> bool:
    """Check if a filter with this name already exists for this user."""
    data = _load_data(chat_id)
    return any(
        f["name"].lower() == name.lower()
        for f in data["filters"].values()
    )


def add_filter(chat_id: str, name: str, params: dict, platform: str = "encar") -> dict | str:
    """
    Add a new filter for a user and return it.
    Returns an error string if the name is already taken.
    """
    data = _load_data(chat_id)

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
        "seen_ids": {},
        "created_at": datetime.now().isoformat(),
        "last_checked": None,
        "total_found": 0,
    }
    data["filters"][filter_id] = new_filter
    _save_data(chat_id, data)
    logger.info(f"Added filter '{name}' ({filter_id}) for user {chat_id}")
    return new_filter


def remove_filter(chat_id: str, id_or_name: str) -> str | None:
    data = _load_data(chat_id)
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        name = data["filters"][fid]["name"]
        del data["filters"][fid]
        _save_data(chat_id, data)
        logger.info(f"Removed filter '{name}' ({fid}) for user {chat_id}")
        return name
    return None


def pause_filter(chat_id: str, id_or_name: str) -> str | None:
    data = _load_data(chat_id)
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        data["filters"][fid]["active"] = False
        _save_data(chat_id, data)
        return data["filters"][fid]["name"]
    return None


def resume_filter(chat_id: str, id_or_name: str) -> str | None:
    data = _load_data(chat_id)
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        data["filters"][fid]["active"] = True
        _save_data(chat_id, data)
        return data["filters"][fid]["name"]
    return None


def get_all_filters(chat_id: str) -> list[dict]:
    data = _load_data(chat_id)
    return list(data["filters"].values())


def get_active_filters(chat_id: str) -> list[dict]:
    return [f for f in get_all_filters(chat_id) if f["active"]]


def get_filter(chat_id: str, id_or_name: str) -> dict | None:
    data = _load_data(chat_id)
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        return data["filters"][fid]
    return None


def _normalize_seen(raw) -> dict:
    """Convert old list format to new dict format {id: matched_bool}."""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(x): True for x in raw}
    return {}


def get_seen_ids(chat_id: str, filter_id: str) -> set:
    data = _load_data(chat_id)
    f = data["filters"].get(filter_id)
    if f:
        return set(_normalize_seen(f.get("seen_ids", {})).keys())
    return set()


def get_seen_cars(chat_id: str, filter_id: str) -> dict:
    data = _load_data(chat_id)
    f = data["filters"].get(filter_id)
    if f:
        return _normalize_seen(f.get("seen_ids", {}))
    return {}


def update_seen_ids(chat_id: str, filter_id: str, new_ids: dict | set, total_new: int = 0) -> None:
    data = _load_data(chat_id)
    if filter_id in data["filters"]:
        existing = _normalize_seen(data["filters"][filter_id].get("seen_ids", {}))
        if isinstance(new_ids, dict):
            existing.update({str(k): v for k, v in new_ids.items()})
        else:
            existing.update({str(x): True for x in new_ids})
        data["filters"][filter_id]["seen_ids"] = existing
        data["filters"][filter_id]["last_checked"] = datetime.now().isoformat()
        data["filters"][filter_id]["total_found"] = (
            data["filters"][filter_id].get("total_found", 0) + total_new
        )
        _save_data(chat_id, data)


def clear_seen_ids(chat_id: str, id_or_name: str) -> str | None:
    data = _load_data(chat_id)
    fid = _resolve_filter_id(data, id_or_name)
    if fid:
        data["filters"][fid]["seen_ids"] = {}
        _save_data(chat_id, data)
        return data["filters"][fid]["name"]
    return None
