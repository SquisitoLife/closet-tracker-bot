import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.markdown import hbold
from aiogram import Router

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# === DB INIT ===
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

# === FSM ===
class AddClothes(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()

@router.message(commands=["start"])
async def cmd_start(message: Message):
    await message.answer("Привет! Я помогу тебе отслеживать одежду. Используй /add для добавления вещей.")

@router.message(commands=["add"])
async def cmd_add(message: Message, state: FSMContext):
    await message.answer("Введи название вещи:")
    await state.set_state(AddClothes.waiting_for_name)

@router.message(AddClothes.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddClothes.waiting_for_category)
    await message.answer("Укажи категорию:")

@router.message(AddClothes.waiting_for_category)
async def process_category(message: Message, state: FSMContext):
    data = await state.get_data()
    cursor.execute("""
        INSERT INTO clothes (user_id, name, category, last_worn, last_washed, worn_count)
        VALUES (?, ?, ?, NULL, NULL, 0)
    """, (message.from_user.id, data['name'], message.text))
    db.commit()
    await message.answer(f"Добавлена вещь: {data['name']} ({message.text})")
    await state.clear()

@router.message(commands=["wear"])
async def cmd_wear(message: Message):
    cursor.execute("SELECT name FROM clothes WHERE user_id = ?", (message.from_user.id,))
    items = cursor.fetchall()
    if not items:
        await message.answer("Нет добавленных вещей. Используй /add")
        return
    buttons = [KeyboardButton(text=item[0]) for item in items]
    kb = ReplyKeyboardMarkup(keyboard=[buttons], resize_keyboard=True)
    await message.answer("Что ты надел?", reply_markup=kb)

@router.message(F.text)
async def mark_worn(message: Message):
    cursor.execute("SELECT id FROM clothes WHERE user_id = ? AND name = ?", (message.from_user.id, message.text))
    item = cursor.fetchone()
    if item:
        now = datetime.now().isoformat()
        cursor.execute("UPDATE clothes SET last_worn = ?, worn_count = worn_count + 1 WHERE id = ?", (now, item[0]))
        db.commit()
        await message.answer(f"Отмечено: ты носил '{message.text}' сегодня.", reply_markup=types.ReplyKeyboardRemove())

@router.message(commands=["wash"])
async def cmd_wash(message: Message):
    cursor.execute("SELECT name FROM clothes WHERE user_id = ?", (message.from_user.id,))
    items = cursor.fetchall()
    if not items:
        await message.answer("Нет добавленных вещей. Используй /add")
        return
    buttons = [KeyboardButton(text=item[0]) for item in items]
    kb = ReplyKeyboardMarkup(keyboard=[buttons], resize_keyboard=True)
    await message.answer("Что ты постирал?", reply_markup=kb)

@router.message(commands=["status"])
async def cmd_status(message: Message):
    cursor.execute("SELECT name, last_worn, last_washed, worn_count FROM clothes WHERE user_id = ?", (message.from_user.id,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет вещей. Используй /add")
        return
    lines = []
    for name, worn, washed, count in rows:
        line = f"👕 <b>{name}</b>\n  - Надевалось: {count} раз\n  - Последний раз носил: {worn or 'никогда'}\n  - Последняя стирка: {washed or 'никогда'}"
        if count >= 3:
            line += "\n  ❗ Пора постирать!"
        lines.append(line)
    await message.answer("\n\n".join(lines))

@router.message(F.text)
async def mark_washed(message: Message):
    cursor.execute("SELECT id FROM clothes WHERE user_id = ? AND name = ?", (message.from_user.id, message.text))
    item = cursor.fetchone()
    if item:
        now = datetime.now().isoformat()
        cursor.execute("UPDATE clothes SET last_washed = ?, worn_count = 0 WHERE id = ?", (now, item[0]))
        db.commit()
        await message.answer(f"'{message.text}' отмечена как чистая!", reply_markup=types.ReplyKeyboardRemove())

async def main():
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
