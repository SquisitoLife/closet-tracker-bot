from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    BotCommand,
)

# =========================
#  Конфигурация и Инициализация
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)  # убирает DeprecationWarning
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# =========================
#  База данных (SQLite)
# =========================

db = sqlite3.connect("closet.db")
cursor = db.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS clothes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    category TEXT,
    last_worn TEXT,
    last_washed TEXT,
    worn_count INTEGER
)
""")
db.commit()

# =========================
#  Вспомогалки
# =========================

def _now_iso() -> str:
    # Всегда записываем в UTC — потом проще сравнивать
    return datetime.now(timezone.utc).isoformat()

def _rows_to_names(rows):
    return [row[0] for row in rows]

def _make_kb_names(names):
    if not names:
        return None
    # Разобьём на строки по 2–3 кнопки, чтобы не растягивалась в одну строку
    row = []
    keyboard = []
    for i, n in enumerate(names, 1):
        row.append(KeyboardButton(text=n))
        if i % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

async def set_my_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Старт"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="add", description="Добавить вещь"),
        BotCommand(command="wear", description="Отметить, что надел"),
        BotCommand(command="wash", description="Отметить, что постирал"),
        BotCommand(command="status", description="Состояние вещей"),
    ])

# =========================
#  Состояния FSM
# =========================

class AddClothes(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()

class WearFlow(StatesGroup):
    choosing_item = State()

class WashFlow(StatesGroup):
    choosing_item = State()

# =========================
#  Хэндлеры
# =========================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я помогу отслеживать одежду и стирку.\n\n"
        "Команды:\n"
        "• /add — добавить вещь\n"
        "• /wear — отметить, что надел вещь\n"
        "• /wash — отметить, что постирал вещь\n"
        "• /status — сводка по вещам\n"
        "• /help — краткая справка"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ Как пользоваться:\n"
        "1) /add — добавь вещь (название и категорию).\n"
        "2) /wear — выбери вещь из списка, если сегодня надевал.\n"
        "3) /wash — выбери вещь, когда постирал.\n"
        "4) /status — сводка (когда носил/стирал, сколько раз носил)."
    )

# ---- Добавление вещи ----

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await message.answer("Введи <b>название</b> вещи:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AddClothes.waiting_for_name)

@router.message(AddClothes.waiting_for_name)
async def add_wait_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddClothes.waiting_for_category)
    await message.answer("Укажи <b>категорию</b> (например: футболка, джинсы):")

@router.message(AddClothes.waiting_for_category)
async def add_wait_category(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("name", "").strip()
    category = message.text.strip()
    cursor.execute(
        """
        INSERT INTO clothes (user_id, name, category, last_worn, last_washed, worn_count)
        VALUES (?, ?, ?, NULL, NULL, 0)
        """,
        (message.from_user.id, name, category)
    )
    db.commit()
    await state.clear()
    await message.answer(f"✅ Добавил: <b>{name}</b> (категория: {category})")

# ---- Ношение ----

@router.message(Command("wear"))
async def cmd_wear(message: Message, state: FSMContext):
    cursor.execute("SELECT name FROM clothes WHERE user_id = ?", (message.from_user.id,))
    names = _rows_to_names(cursor.fetchall())
    if not names:
        await message.answer("Пока нет вещей. Используй /add, чтобы добавить.")
        return
    kb = _make_kb_names(names)
    await state.set_state(WearFlow.choosing_item)
    await message.answer("Что ты <b>надел</b>?", reply_markup=kb)

@router.message(WearFlow.choosing_item, F.text)
async def wear_choose_item(message: Message, state: FSMContext):
    cursor.execute(
        "SELECT id FROM clothes WHERE user_id = ? AND name = ?",
        (message.from_user.id, message.text.strip())
    )
    row = cursor.fetchone()
    if not row:
        await message.answer("Не нашёл такую вещь. Выбери из клавиатуры или напиши точное название.")
        return
    item_id = row[0]
    now = _now_iso()
    cursor.execute(
        "UPDATE clothes SET last_worn = ?, worn_count = worn_count + 1 WHERE id = ?",
        (now, item_id)
    )
    db.commit()
    await state.clear()
    await message.answer(
        f"👕 Отметил: сегодня носил «{message.text.strip()}».",
        reply_markup=ReplyKeyboardRemove()
    )

# ---- Стирка ----

@router.message(Command("wash"))
async def cmd_wash(message: Message, state: FSMContext):
    cursor.execute("SELECT name FROM clothes WHERE user_id = ?", (message.from_user.id,))
    names = _rows_to_names(cursor.fetchall())
    if not names:
        await message.answer("Пока нет вещей. Используй /add, чтобы добавить.")
        return
    kb = _make_kb_names(names)
    await state.set_state(WashFlow.choosing_item)
    await message.answer("Что ты <b>постирал</b>?", reply_markup=kb)

@router.message(WashFlow.choosing_item, F.text)
async def wash_choose_item(message: Message, state: FSMContext):
    cursor.execute(
        "SELECT id FROM clothes WHERE user_id = ? AND name = ?",
        (message.from_user.id, message.text.strip())
    )
    row = cursor.fetchone()
    if not row:
        await message.answer("Не нашёл такую вещь. Выбери из клавиатуры или напиши точное название.")
        return
    item_id = row[0]
    now = _now_iso()
    cursor.execute(
        "UPDATE clothes SET last_washed = ?, worn_count = 0 WHERE id = ?",
        (now, item_id)
    )
    db.commit()
    await state.clear()
    await message.answer(
        f"🧼 Отметил: «{message.text.strip()}» чистая!",
        reply_markup=ReplyKeyboardRemove()
    )

# ---- Статус ----

@router.message(Command("status"))
async def cmd_status(message: Message):
    cursor.execute(
        "SELECT name, last_worn, last_washed, worn_count FROM clothes WHERE user_id = ?",
        (message.from_user.id,)
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет вещей. Добавь через /add.")
        return

    lines = []
    for name, worn, washed, count in rows:
        line = (
            f"👕 <b>{name}</b>\n"
            f"  • Надевалось: {count} раз\n"
            f"  • Последний раз носил: {worn or 'никогда'}\n"
            f"  • Последняя стирка: {washed or 'никогда'}"
        )
        # Простое правило для подсказки о стирке
        if count >= 3:
            line += "\n  ❗ Пора постирать!"
        lines.append(line)

    await message.answer("\n\n".join(lines))

# =========================
#  Запуск
# =========================

async def main():
    await set_my_commands()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
