"""
Jarvis 2.0 — Telegram бот для студентов СРМК
Функции:
1. 📋 Расписание занятий группы (без авторизации)
2. 👁️ Поиск преподавателя по всем группам (без авторизации)
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import List, Dict, Tuple, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientSession, TCPConnector, ClientTimeout
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from groups import GROUPS

load_dotenv()

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Белый список Telegram ID (через запятую в .env, например: "2035205294,123456789")
# Если переменная пустая — бот доступен всем
raw_allowed = os.getenv("ALLOWED_USERS", "2035205294").strip()
ALLOWED_USERS = [uid.strip() for uid in raw_allowed.split(",") if uid.strip()]

# Настройки Moodle
BASE_URL = "https://rmk.stavedu.ru:8010/moodle"
TIMETABLE_URL = f"{BASE_URL}/eioswork/timetable/watchstudent.php"

# ID группы по умолчанию (238 = П-31 / П-21)
DEFAULT_GROUP_ID = os.getenv("DEFAULT_GROUP_ID", "238").strip()

# Расписание звонков
TIMES = {
    "1": "8:00 - 9:30",
    "2": "9:40 - 11:10",
    "3": "11:40 - 13:10",
    "4": "13:20 - 14:50",
    "5": "15:00 - 16:30",
    "6": "16:50 - 18:20",
    "7": "18:30 - 20:00"
}

dp = Dispatcher(storage=MemoryStorage())

# ==================== СОСТОЯНИЯ FSM ====================
class BotStates(StatesGroup):
    waiting_teacher_name = State()

# ==================== КЛАВИАТУРЫ ====================
def main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура бота"""
    buttons = [
        [KeyboardButton(text="📋 Расписание"), KeyboardButton(text="👁️ Поиск преподавателя")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены"""
    buttons = [[KeyboardButton(text="❌ Отмена")]]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ==================== ПРОВЕРКА ДОСТУПА ====================
def is_user_allowed(user_id: int | str) -> bool:
    """Проверяет, есть ли пользователь в белом списке"""
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS

async def check_access(message: Message) -> bool:
    """Проверка доступа с отправкой сообщения при отказе"""
    if not is_user_allowed(message.from_user.id):
        await message.answer("❌ Доступ запрещён. Ваш Telegram ID не в белом списке бота.")
        return False
    return True

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
async def send_long_message(message: Message, text: str, reply_markup=None, parse_mode="Markdown"):
    """Отправляет длинные сообщения частями (лимит Telegram 4096 символов)"""
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return

    # Разбиваем по строкам
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            if chunk:
                await message.answer(chunk, parse_mode=parse_mode)
                chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk, reply_markup=reply_markup, parse_mode=parse_mode)

# ==================== ПАРСИНГ РАСПИСАНИЯ ====================
def parse_timetable(html: str) -> str:
    """Парсит HTML страницу расписания группы"""
    soup = BeautifulSoup(html, "html.parser")
    day_tables = soup.find_all("table", class_="daytable")

    if not day_tables:
        return "❌ Расписание на текущий месяц не найдено."

    # Извлекаем заголовок группы (например: "Группа П-31")
    header_tag = soup.find(["h1", "h2", "h3", "h4"])
    group_title = header_tag.get_text(strip=True) if header_tag else "П-31"

    result = [f"📅 *Расписание занятий ({group_title})*\n"]

    for day_table in day_tables:
        day_header = day_table.find("td", class_="thead")
        if day_header:
            day_text = day_header.get_text(strip=True)
            result.append(f"\n*{day_text}:*")

        rows = day_table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            pair_num = cells[0].get_text(strip=True)
            rowtable = cells[1].find("table", class_="rowtable")
            if not rowtable:
                continue

            pair_rows = rowtable.find_all("tr")
            if not pair_rows:
                continue

            first_row = pair_rows[0]
            pair_cells = first_row.find_all("td")
            if not pair_cells:
                continue

            pair_info = pair_cells[0].get_text(strip=True)

            if "—" in pair_info and pair_info.count("—") >= 2:
                continue

            parts = pair_info.split("|")
            if len(parts) >= 2:
                subject = parts[0].strip()
                teacher = parts[1].strip()
                cabinet = pair_cells[1].get_text(strip=True) if len(pair_cells) > 1 else "—"

                result.append(f"{pair_num}) {subject}")
                result.append(f"├ ⏰ Время: `{TIMES.get(pair_num, '—')}`")
                result.append(f"├ 👤 Преподаватель: {teacher}")
                result.append(f"└ 🚪 Кабинет: {cabinet}")

                # Подгруппа 2 при наличии
                if len(pair_rows) > 1:
                    second_row = pair_rows[1]
                    second_cells = second_row.find_all("td")
                    if second_cells:
                        second_info = second_cells[0].get_text(strip=True)
                        if "—" not in second_info or second_info.count("—") < 2:
                            second_parts = second_info.split("|")
                            if len(second_parts) >= 2:
                                second_teacher = second_parts[1].strip()
                                second_cabinet = second_cells[1].get_text(strip=True) if len(second_cells) > 1 else "—"
                                result.append(f"└ 👥 Подгруппа 2: {second_teacher} | Каб: {second_cabinet}")

    return "\n".join(result) if len(result) > 1 else "📭 Расписание не найдено."

async def fetch_timetable_public(group_id: str = DEFAULT_GROUP_ID) -> str:
    """Загружает и парсит расписание для указанной группы без авторизации"""
    now = datetime.now()
    url = f"{TIMETABLE_URL}?year={now.year}&month={now.month}&group={group_id}"

    timeout = ClientTimeout(total=25, connect=10)
    connector = TCPConnector(ssl=False, force_close=True)

    try:
        async with ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    return parse_timetable(html)
                return "❌ Сервер расписания вернул ошибку. Попробуйте позже."
    except Exception as e:
        logging.error(f"Ошибка получения расписания: {e}")
        return "❌ Сервер Moodle временно недоступен. Попробуйте позже."

# ==================== ПОИСК ПРЕПОДАВАТЕЛЕЙ ====================
async def fetch_group_timetable_html(session: ClientSession, group_id: str) -> Tuple[str, Optional[str]]:
    """Получает HTML расписания для конкретной группы"""
    now = datetime.now()
    url = f"{TIMETABLE_URL}?year={now.year}&month={now.month}&group={group_id}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return (group_id, await resp.text())
            return (group_id, None)
    except:
        return (group_id, None)

async def search_teacher_public(teacher_name: str) -> str:
    """Параллельно ищет преподавателя по всем группам"""
    schedule_by_day: Dict[str, list] = {}
    teacher_query = teacher_name.strip().lower()

    timeout = ClientTimeout(total=45, connect=10)
    connector = TCPConnector(ssl=False, limit=25, force_close=True)

    group_names = {gid: gname for gname, gid in GROUPS.items()}
    all_groups = list(GROUPS.items())
    all_results = {}

    try:
        async with ClientSession(connector=connector, timeout=timeout) as session:
            # Первый проход: параллельный сбор со всех групп
            tasks = [fetch_group_timetable_html(session, gid) for _, gid in all_groups]
            results = await asyncio.gather(*tasks)

            for group_id, html in results:
                all_results[group_id] = html

            # Повторный запрос для групп, завершившихся с ошибкой сети
            failed_groups = [gid for gid, html in results if not html]
            if failed_groups:
                retry_tasks = [fetch_group_timetable_html(session, gid) for gid in failed_groups]
                retry_results = await asyncio.gather(*retry_tasks)
                for group_id, html in retry_results:
                    if html:
                        all_results[group_id] = html
    except Exception as e:
        logging.error(f"Ошибка при поиске преподавателя: {e}")

    # Парсим собранные страницы
    for group_id, html in all_results.items():
        if not html:
            continue

        group_name = group_names.get(group_id, group_id)
        try:
            soup = BeautifulSoup(html, "html.parser")
            day_tables = soup.find_all("table", class_="daytable")

            for day_table in day_tables:
                day_header = day_table.find("td", class_="thead")
                if not day_header:
                    continue

                day_text = day_header.get_text(strip=True)
                rows = day_table.find_all("tr")[1:]

                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue

                    pair_num = cells[0].get_text(strip=True)
                    rowtable = cells[1].find("table", class_="rowtable")
                    if not rowtable:
                        continue

                    pair_rows = rowtable.find_all("tr")
                    if not pair_rows:
                        continue

                    # Подгруппа 1
                    first_row = pair_rows[0]
                    pair_cells = first_row.find_all("td")
                    if pair_cells:
                        pair_info = pair_cells[0].get_text(strip=True)
                        if not ("—" in pair_info and pair_info.count("—") >= 2):
                            parts = pair_info.split("|")
                            if len(parts) >= 2:
                                subject = parts[0].strip()
                                teacher = parts[1].strip()
                                cabinet = pair_cells[1].get_text(strip=True) if len(pair_cells) > 1 else "—"

                                if teacher_query in teacher.lower() and cabinet != "—":
                                    schedule_by_day.setdefault(day_text, []).append({
                                        "group": group_name,
                                        "pair_num": int(pair_num) if pair_num.isdigit() else pair_num,
                                        "subject": subject,
                                        "teacher": teacher,
                                        "cabinet": cabinet
                                    })

                    # Подгруппа 2
                    if len(pair_rows) > 1:
                        second_row = pair_rows[1]
                        second_cells = second_row.find_all("td")
                        if second_cells:
                            second_info = second_cells[0].get_text(strip=True)
                            if not ("—" in second_info and second_info.count("—") >= 2):
                                second_parts = second_info.split("|")
                                if len(second_parts) >= 2:
                                    second_subject = second_parts[0].strip()
                                    second_teacher = second_parts[1].strip()
                                    second_cabinet = second_cells[1].get_text(strip=True) if len(second_cells) > 1 else "—"

                                    if teacher_query in second_teacher.lower() and second_cabinet != "—":
                                        schedule_by_day.setdefault(day_text, []).append({
                                            "group": f"{group_name} (п/г 2)",
                                            "pair_num": int(pair_num) if pair_num.isdigit() else pair_num,
                                            "subject": second_subject,
                                            "teacher": second_teacher,
                                            "cabinet": second_cabinet
                                        })
        except Exception:
            continue

    if not schedule_by_day:
        return f"❌ Преподаватель *{teacher_name}* не найден в расписании на этот месяц."

    result = [f"🔍 *Поиск преподавателя:* {teacher_name}\n"]

    def get_day_sort_key(day_str: str) -> int:
        try:
            day_num = int(day_str.split()[0])
            now_day = datetime.now().day
            if day_num < now_day:
                return day_num + 100
            return day_num
        except:
            return 999

    sorted_days = sorted(schedule_by_day.keys(), key=get_day_sort_key)
    unique_groups = set()
    total_pairs = 0

    for day in sorted_days:
        result.append(f"\n📅 *{day}:*")
        pairs = sorted(
            schedule_by_day[day],
            key=lambda x: x["pair_num"] if isinstance(x["pair_num"], int) else 99
        )
        for pair in pairs:
            total_pairs += 1
            unique_groups.add(pair["group"].split()[0])
            time_str = TIMES.get(str(pair["pair_num"]), "—")
            result.append(
                f"• {pair['group']} | *{pair['pair_num']} пара* (`{time_str}`)\n"
                f"  └ 📖 {pair['subject']} — каб. *{pair['cabinet']}*"
            )

    result.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    result.append(f"📊 *Всего найдено:* `{total_pairs}` пар в `{len(unique_groups)}` группах")

    return "\n".join(result)

# ==================== ХЭНДЛЕРЫ КОМАНД ====================
@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработка /start и /help"""
    if not await check_access(message):
        return

    await state.clear()
    welcome_text = (
        "👋 *Привет! Я Jarvis 2.0* — бот расписания СРМК.\n\n"
        "✨ *Доступные функции:*\n"
        "• 📋 *Расписание* — расписание занятий группы\n"
        "• 👁️ *Поиск преподавателя* — найти пары любого преподавателя по всем группам\n\n"
        "⚡ _Работает быстро и без авторизации в Moodle!_\n\n"
        "Выберите действие в меню ниже 👇"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard())

@dp.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    if not await check_access(message):
        return

    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_keyboard())

# ==================== ОБРАБОТКА КНОПОК И СООБЩЕНИЙ ====================
@dp.message(F.text == "📋 Расписание")
async def handle_timetable(message: Message, state: FSMContext):
    """Кнопка расписания"""
    if not await check_access(message):
        return

    await state.clear()
    wait_msg = await message.answer("⏳ Загружаю расписание...")
    result = await fetch_timetable_public(DEFAULT_GROUP_ID)
    try:
        await wait_msg.delete()
    except:
        pass
    await send_long_message(message, result, reply_markup=main_keyboard())

@dp.message(F.text == "👁️ Поиск преподавателя")
async def handle_teacher_search_prompt(message: Message, state: FSMContext):
    """Запрос фамилии преподавателя"""
    if not await check_access(message):
        return

    await state.set_state(BotStates.waiting_teacher_name)
    await message.answer(
        "👁️ *Поиск преподавателя*\n\n"
        "Введите фамилию (или часть фамилии) преподавателя:\n"
        "*(Например: Иванов или Иванова)*",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.message(BotStates.waiting_teacher_name)
async def handle_teacher_search_execute(message: Message, state: FSMContext):
    """Выполнение поиска преподавателя"""
    if not await check_access(message):
        return

    query = message.text.strip()
    if len(query) < 2:
        await message.answer("❌ Введите хотя бы 2 буквы фамилии:", reply_markup=cancel_keyboard())
        return

    wait_msg = await message.answer("⏳ Ищу преподавателя по всем группам СРМК...\n_Это займёт 5-15 секунд._", parse_mode="Markdown")
    result = await search_teacher_public(query)
    await state.clear()

    try:
        await wait_msg.delete()
    except:
        pass

    await send_long_message(message, result, reply_markup=main_keyboard())

@dp.message()
async def handle_unknown(message: Message):
    """Обработка неизвестных сообщений"""
    if not await check_access(message):
        return
    await message.answer("Выберите действие из меню:", reply_markup=main_keyboard())

# ==================== ТОЧКА ВХОДА ====================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN не указан в .env файле!")
        print("❌ Ошибка: Укажите BOT_TOKEN в файле .env")
        return

    bot = Bot(token=BOT_TOKEN, session=AiohttpSession())

    try:
        print("🤖 Jarvis 2.0 запущен и готов к работе!")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
