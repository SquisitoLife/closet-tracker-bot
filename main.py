import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from aiogram.dispatcher.filters import Command
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup
import sqlite3
import os
from datetime import datetime, timedelta

API_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

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

class AddClothes(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    await message.answer("Привет! Я помогу тебе следить за одеждой. Используй /add чтобы добавить вещь.")

@dp.message_handler(commands=["add"])
async def add_cmd(message: types.Message):
    await message.answer("Введи название вещи (например, 'Футболка H&M'):")
    await AddClothes.waiting_for_name.set()

@dp.message_handler(state=AddClothes.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Укажи категорию (футболка, джинсы и т.д.):")
    await AddClothes.waiting_for_category.set()

@dp.message_handler(state=AddClothes.waiting_for_category)
async def process_category(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cursor.execute("""
        INSERT INTO clothes (user_id, name, category, last_worn, last_washed, worn_count)
        VALUES (?, ?, ?, NULL, NULL, 0)
    """, (message.from_user.id, data['name'], message.text))
    db.commit()
    await message.answer(f"Добавлена вещь: {data['name']} ({message.text})")
    await state.finish()

@dp.message_handler(commands=["wear"])
async def wear_cmd(message: types.Message):
    cursor.execute("SELECT id, name FROM clothes WHERE user_id = ?", (message.from_user.id,))
    items = cursor.fetchall()
    if not items:
        await message.answer("Ты ещё не добавил вещи. Используй /add")
        return
    buttons = [KeyboardButton(text=item[1]) for item in items]
    kb = ReplyKeyboardMarkup(resize_keyboard=True).add(*buttons)
    await message.answer("Что ты надел?", reply_markup=kb)

@dp.message_handler(lambda msg: True, content_types=types.ContentType.TEXT)
async def mark_worn(message: types.Message):
    cursor.execute("SELECT id FROM clothes WHERE user_id = ? AND name = ?", (message.from_user.id, message.text))
    result = cursor.fetchone()
    if result:
        now = datetime.now().isoformat()
        cursor.execute("UPDATE clothes SET last_worn = ?, worn_count = worn_count + 1 WHERE id = ?", (now, result[0]))
        db.commit()
        await message.answer(f"Отмечено: ты носил '{message.text}' сегодня.", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(commands=["wash"])
async def wash_cmd(message: types.Message):
    cursor.execute("SELECT id, name FROM clothes WHERE user_id = ?", (message.from_user.id,))
    items = cursor.fetchall()
    if not items:
        await message.answer("Добавь хотя бы одну вещь через /add")
        return
    buttons = [KeyboardButton(text=item[1]) for item in items]
    kb = ReplyKeyboardMarkup(resize_keyboard=True).add(*buttons)
    await message.answer("Что ты постирал?", reply_markup=kb)

@dp.message_handler(commands=["status"])
async def status_cmd(message: types.Message):
    cursor.execute("SELECT name, last_worn, last_washed, worn_count FROM clothes WHERE user_id = ?", (message.from_user.id,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("У тебя пока нет вещей. Используй /add")
        return
    report = "\n".join([
        f"👕 {name}:\n  - Надевалось: {worn_count} раз\n  - Последний раз носил: {last_worn or 'никогда'}\n  - Последняя стирка: {last_washed or 'никогда'}" +
        ("\n  ❗ Пора постирать!" if worn_count >= 3 else "")
        for name, last_worn, last_washed, worn_count in rows
    ])
    await message.answer(report)

@dp.message_handler(lambda msg: True)
async def mark_washed(message: types.Message):
    cursor.execute("SELECT id FROM clothes WHERE user_id = ? AND name = ?", (message.from_user.id, message.text))
    result = cursor.fetchone()
    if result:
        now = datetime.now().isoformat()
        cursor.execute("UPDATE clothes SET last_washed = ?, worn_count = 0 WHERE id = ?", (now, result[0]))
        db.commit()
        await message.answer(f"Ок, '{message.text}' теперь чистая!", reply_markup=types.ReplyKeyboardRemove())

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)