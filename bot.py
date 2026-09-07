"""
Jarvis 2.0 — Telegram бот для студентов СРМК
Функции:
1. 📋 Расписание занятий группы (выбор группы через текстовый ввод)
2. 🔄 Сменить группу — поменять сохранённую группу
3. 👁️ Поиск преподавателя — только для администраторов
4. 📢 Рассылка — отправка сообщений всем, только для администраторов
5. 📊 Статистика — аналитика, только для администраторов
"""

import asyncio
import difflib
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
import storage

load_dotenv()

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Список Telegram ID администраторов (через запятую в .env)
# Пример: ADMIN_IDS=2035205294,123456789
raw_admins = os.getenv("ADMIN_IDS", "2035205294").strip()
ADMIN_IDS: list[str] = [uid.strip() for uid in raw_admins.split(",") if uid.strip()]

# Настройки Moodle
BASE_URL = "https://rmk.stavedu.ru:8010/moodle"
TIMETABLE_URL = f"{BASE_URL}/eioswork/timetable/watchstudent.php"

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
    waiting_group = State()
    waiting_broadcast_text = State()
    waiting_broadcast_confirm = State()


# ==================== ПРОВЕРКА ПРАВ ====================
def is_admin(user_id: int | str) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return str(user_id) in ADMIN_IDS


# ==================== КЛАВИАТУРЫ ====================
def user_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура обычного пользователя."""
    buttons = [
        [KeyboardButton(text="📋 Расписание"), KeyboardButton(text="🔄 Сменить группу")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def admin_keyboard() -> ReplyKeyboardMarkup:
    """Расширенная клавиатура администратора."""
    buttons = [
        [KeyboardButton(text="📋 Расписание"), KeyboardButton(text="🔄 Сменить группу")],
        [KeyboardButton(text="👁️ Поиск преподавателя")],
        [KeyboardButton(text="📢 Рассылка"), KeyboardButton(text="📊 Статистика")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_main_keyboard(user_id: int | str) -> ReplyKeyboardMarkup:
    """Возвращает подходящую клавиатуру в зависимости от роли."""
    return admin_keyboard() if is_admin(user_id) else user_keyboard()


def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены."""
    buttons = [[KeyboardButton(text="❌ Отмена")]]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def confirm_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура подтверждения рассылки."""
    buttons = [
        [KeyboardButton(text="✅ Да, отправить"), KeyboardButton(text="❌ Нет, отмена")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
async def send_long_message(message: Message, text: str, reply_markup=None, parse_mode="Markdown"):
    """Отправляет длинные сообщения частями (лимит Telegram 4096 символов)."""
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return

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
    """Парсит HTML страницу расписания группы."""
    soup = BeautifulSoup(html, "html.parser")
    day_tables = soup.find_all("table", class_="daytable")

    if not day_tables:
        return "❌ Расписание на текущий месяц не найдено."

    # Извлекаем заголовок группы (например: "Группа П-31")
    header_tag = soup.find(["h1", "h2", "h3", "h4"])
    group_title = header_tag.get_text(strip=True) if header_tag else "Группа"

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


async def fetch_timetable_public(group_id: str) -> str:
    """Загружает и парсит расписание для указанной группы без авторизации."""
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
    """Получает HTML расписания для конкретной группы."""
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
    """Параллельно ищет преподавателя по всем группам."""
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
    """Обработка /start и /help — регистрирует пользователя, показывает меню."""
    await state.clear()
    user = message.from_user
    await storage.register_user(user.id, user.username)

    welcome_text = (
        "👋 *Привет! Я Jarvis 2.0* — бот расписания СРМК.\n\n"
        "✨ *Доступные функции:*\n"
        "• 📋 *Расписание* — расписание занятий твоей группы\n"
        "• 🔄 *Сменить группу* — изменить выбранную группу\n"
    )
    if is_admin(user.id):
        welcome_text += (
            "• 👁️ *Поиск преподавателя* — найти пары любого преподавателя\n"
            "• 📢 *Рассылка* — отправить сообщение всем пользователям\n"
            "• 📊 *Статистика* — аналитика бота\n"
        )
    welcome_text += "\n⚡ _Работает быстро и без авторизации в Moodle!_\n\nВыберите действие в меню ниже 👇"

    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard(user.id))


@dp.message(F.text == "❌ Отмена")
@dp.message(F.text == "❌ Нет, отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена текущего действия."""
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard(message.from_user.id))


# ==================== РАСПИСАНИЕ ====================
@dp.message(F.text == "📋 Расписание")
async def handle_timetable(message: Message, state: FSMContext):
    """Кнопка расписания — если группа не выбрана, запрашивает её."""
    await state.clear()
    user_id = message.from_user.id
    group_id = await storage.get_user_group(user_id)

    if not group_id:
        await message.answer(
            "📋 *Расписание*\n\n"
            "Ты ещё не выбрал свою группу.\n"
            "Введи название группы _(например: П-21)_:",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )
        await state.set_state(BotStates.waiting_group)
        return

    wait_msg = await message.answer("⏳ Загружаю расписание...")
    result = await fetch_timetable_public(group_id)
    await storage.increment_views()

    try:
        await wait_msg.delete()
    except:
        pass

    await send_long_message(message, result, reply_markup=get_main_keyboard(user_id))


@dp.message(F.text == "🔄 Сменить группу")
async def handle_change_group(message: Message, state: FSMContext):
    """Кнопка смены группы — запрашивает новую группу текстом."""
    await state.clear()
    await message.answer(
        "🔄 *Смена группы*\n\n"
        "Введи название своей группы _(например: П-21)_:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(BotStates.waiting_group)


@dp.message(BotStates.waiting_group)
async def process_group_input(message: Message, state: FSMContext):
    """Обрабатывает ввод группы. При опечатке — подсказывает через difflib."""
    raw = message.text.strip()
    group_name = raw.upper()
    group_id = GROUPS.get(group_name)
    user_id = message.from_user.id

    if group_id:
        await storage.set_user_group(user_id, group_id, group_name)
        await state.clear()

        # Сразу показываем расписание
        wait_msg = await message.answer(
            f"✅ Группа *{group_name}* сохранена!\n\n⏳ Загружаю расписание...",
            parse_mode="Markdown"
        )
        result = await fetch_timetable_public(group_id)
        await storage.increment_views()

        try:
            await wait_msg.delete()
        except:
            pass

        await send_long_message(message, result, reply_markup=get_main_keyboard(user_id))
    else:
        # Ищем похожие группы через difflib
        close = difflib.get_close_matches(group_name, GROUPS.keys(), n=3, cutoff=0.6)
        if close:
            suggestions = ", ".join(f"`{g}`" for g in close)
            await message.answer(
                f"❌ Группа *{group_name}* не найдена.\n\n"
                f"💡 Возможно, ты имел в виду: {suggestions}?\n\n"
                "Введи название ещё раз:",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard()
            )
        else:
            await message.answer(
                f"❌ Группа *{group_name}* не найдена.\n\n"
                "Проверь правильность написания _(например: П-21, КС-11, Ю-32)_ и попробуй ещё раз:",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard()
            )


# ==================== ПОИСК ПРЕПОДАВАТЕЛЯ (ТОЛЬКО АДМИН) ====================
@dp.message(F.text == "👁️ Поиск преподавателя")
async def handle_teacher_search_prompt(message: Message, state: FSMContext):
    """Запрос фамилии преподавателя — только для администраторов."""
    if not is_admin(message.from_user.id):
        await message.answer("Выберите действие из меню:", reply_markup=get_main_keyboard(message.from_user.id))
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
    """Выполнение поиска преподавателя."""
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    query = message.text.strip()
    if len(query) < 2:
        await message.answer("❌ Введите хотя бы 2 буквы фамилии:", reply_markup=cancel_keyboard())
        return

    wait_msg = await message.answer(
        "⏳ Ищу преподавателя по всем группам СРМК...\n_Это займёт 5-15 секунд._",
        parse_mode="Markdown"
    )
    result = await search_teacher_public(query)
    await state.clear()

    try:
        await wait_msg.delete()
    except:
        pass

    await send_long_message(message, result, reply_markup=get_main_keyboard(message.from_user.id))


# ==================== РАССЫЛКА (ТОЛЬКО АДМИН) ====================
@dp.message(F.text == "📢 Рассылка")
async def handle_broadcast_start(message: Message, state: FSMContext):
    """Начало рассылки — только для администраторов."""
    if not is_admin(message.from_user.id):
        await message.answer("Выберите действие из меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return

    await state.set_state(BotStates.waiting_broadcast_text)
    await message.answer(
        "📢 *Рассылка*\n\n"
        "Введите текст сообщения для отправки всем пользователям бота.\n"
        "_Поддерживается Markdown-разметка._",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )


@dp.message(BotStates.waiting_broadcast_text)
async def handle_broadcast_text(message: Message, state: FSMContext):
    """Сохраняет текст рассылки и запрашивает подтверждение."""
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    broadcast_text = message.text.strip()
    if not broadcast_text:
        await message.answer("❌ Текст не может быть пустым. Введите сообщение для рассылки:")
        return

    await state.update_data(broadcast_text=broadcast_text)
    await state.set_state(BotStates.waiting_broadcast_confirm)

    user_count = len(await storage.get_all_user_ids())
    await message.answer(
        f"📋 *Предпросмотр сообщения:*\n\n{broadcast_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Будет отправлено: *{user_count}* пользователям.\n"
        f"Подтверждаешь рассылку?",
        parse_mode="Markdown",
        reply_markup=confirm_keyboard()
    )


@dp.message(BotStates.waiting_broadcast_confirm, F.text == "✅ Да, отправить")
async def handle_broadcast_confirm(message: Message, state: FSMContext, bot: Bot):
    """Выполняет рассылку после подтверждения."""
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    broadcast_text = data.get("broadcast_text", "")
    await state.clear()

    if not broadcast_text:
        await message.answer("❌ Текст рассылки не найден.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    all_user_ids = await storage.get_all_user_ids()
    total = len(all_user_ids)
    sent = 0
    failed = 0

    status_msg = await message.answer(
        f"📤 Начинаю рассылку для {total} пользователей...",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

    for uid in all_user_ids:
        try:
            await bot.send_message(int(uid), broadcast_text, parse_mode="Markdown")
            sent += 1
        except Exception as e:
            failed += 1
            logging.warning(f"Не удалось отправить сообщение пользователю {uid}: {e}")
        # Небольшая задержка чтобы не упереться в лимиты Telegram
        await asyncio.sleep(0.05)

    try:
        await status_msg.delete()
    except:
        pass

    await message.answer(
        f"✅ *Рассылка завершена!*\n\n"
        f"📊 Итог:\n"
        f"• ✅ Доставлено: *{sent}* из *{total}*\n"
        f"• ❌ Не доставлено: *{failed}* _(заблокировали бота или ошибка)_",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(message.from_user.id)
    )


# ==================== СТАТИСТИКА (ТОЛЬКО АДМИН) ====================
@dp.message(F.text == "📊 Статистика")
async def handle_stats(message: Message, state: FSMContext):
    """Отображает статистику бота — только для администраторов."""
    if not is_admin(message.from_user.id):
        await message.answer("Выберите действие из меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return

    await state.clear()
    stats = await storage.get_stats()

    top_groups_text = ""
    if stats["top_groups"]:
        top_groups_text = "\n\n🏆 *Топ-5 популярных групп:*\n"
        for i, (gname, count) in enumerate(stats["top_groups"], 1):
            top_groups_text += f"  {i}. *{gname}* — {count} чел.\n"
    else:
        top_groups_text = "\n\n_Пока ни один пользователь не выбрал группу._"

    text = (
        "📊 *Статистика Jarvis 2.0*\n\n"
        f"👥 Всего пользователей: *{stats['total_users']}*\n"
        f"🎓 Выбрали группу: *{stats['users_with_group']}*\n"
        f"📋 Просмотров расписания: *{stats['schedule_views']}*"
        f"{top_groups_text}"
    )

    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))


# ==================== НЕИЗВЕСТНЫЕ СООБЩЕНИЯ ====================
@dp.message()
async def handle_unknown(message: Message, state: FSMContext):
    """Обработка неизвестных сообщений — регистрирует и показывает меню."""
    user = message.from_user
    await storage.register_user(user.id, user.username)
    await message.answer("Выберите действие из меню:", reply_markup=get_main_keyboard(user.id))


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
