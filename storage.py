"""
storage.py — JSON-хранилище пользователей Jarvis 2.0
Файл: data/users.json
Защита конкурентных записей: asyncio.Lock
"""

import asyncio
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

# ==================== КОНФИГУРАЦИЯ ====================
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "users.json")

_lock = asyncio.Lock()

logger = logging.getLogger(__name__)

# ==================== СТРУКТУРА ПО УМОЛЧАНИЮ ====================
DEFAULT_DATA: dict = {
    "users": {},
    "schedule_views": 0,
}


# ==================== НИЗКОУРОВНЕВЫЕ ОПЕРАЦИИ ====================
async def load_data() -> dict:
    """Читает users.json. Если файла нет — возвращает пустую структуру."""
    async with _lock:
        return _load_data_unsafe()


def _load_data_unsafe() -> dict:
    """Внутреннее чтение без Lock (вызывать только внутри Lock!)."""
    if not os.path.exists(DATA_FILE):
        return {**DEFAULT_DATA, "users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Гарантируем наличие обоих ключей верхнего уровня
        data.setdefault("users", {})
        data.setdefault("schedule_views", 0)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Ошибка чтения {DATA_FILE}: {e}")
        return {**DEFAULT_DATA, "users": {}}


async def save_data(data: dict) -> None:
    """Записывает data в users.json (атомарно через Lock)."""
    async with _lock:
        _save_data_unsafe(data)


def _save_data_unsafe(data: dict) -> None:
    """Внутренняя запись без Lock (вызывать только внутри Lock!)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"Ошибка записи {DATA_FILE}: {e}")


# ==================== ПУБЛИЧНЫЕ ФУНКЦИИ ====================
async def register_user(user_id: int | str, username: Optional[str] = None) -> None:
    """
    Добавляет пользователя в хранилище, если его ещё нет.
    Повторные вызовы (напр., при каждом /start) безопасны — запись не перезаписывается.
    """
    uid = str(user_id)
    async with _lock:
        data = _load_data_unsafe()
        if uid not in data["users"]:
            data["users"][uid] = {
                "group_id": None,
                "group_name": None,
                "username": username,
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
            _save_data_unsafe(data)
            logger.info(f"Новый пользователь зарегистрирован: {uid} (@{username})")


async def set_user_group(user_id: int | str, group_id: str, group_name: str) -> None:
    """Сохраняет выбранную группу пользователя."""
    uid = str(user_id)
    async with _lock:
        data = _load_data_unsafe()
        if uid not in data["users"]:
            # Регистрируем, если вдруг отсутствует
            data["users"][uid] = {
                "group_id": None,
                "group_name": None,
                "username": None,
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
        data["users"][uid]["group_id"] = group_id
        data["users"][uid]["group_name"] = group_name
        _save_data_unsafe(data)


async def get_user_group(user_id: int | str) -> Optional[str]:
    """
    Возвращает сохранённый group_id пользователя или None, если группа не выбрана.
    Не требует Lock на чтение — JSON читается атомарно ОС.
    """
    uid = str(user_id)
    data = _load_data_unsafe()
    user = data["users"].get(uid)
    if user:
        return user.get("group_id")
    return None


async def increment_views() -> None:
    """Увеличивает глобальный счётчик просмотров расписания на 1."""
    async with _lock:
        data = _load_data_unsafe()
        data["schedule_views"] = data.get("schedule_views", 0) + 1
        _save_data_unsafe(data)


async def get_all_user_ids() -> list[str]:
    """Возвращает список всех зарегистрированных user_id (строки)."""
    data = _load_data_unsafe()
    return list(data["users"].keys())


async def get_stats() -> dict:
    """
    Возвращает агрегированную статистику:
    {
        "total_users": int,
        "users_with_group": int,
        "schedule_views": int,
        "top_groups": list[tuple[group_name, count]],  # топ-5
    }
    """
    data = _load_data_unsafe()
    users = data["users"]

    total = len(users)
    with_group = sum(1 for u in users.values() if u.get("group_id"))
    views = data.get("schedule_views", 0)

    group_counter: Counter = Counter()
    for u in users.values():
        gname = u.get("group_name")
        if gname:
            group_counter[gname] += 1

    top_groups = group_counter.most_common(5)

    return {
        "total_users": total,
        "users_with_group": with_group,
        "schedule_views": views,
        "top_groups": top_groups,
    }
