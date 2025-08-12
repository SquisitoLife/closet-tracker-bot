# main.py
from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.client.default import DefaultBotProperties

# ========= Логирование / токен =========
logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# ========= HTTP health (для Render Web Service) =========
async def _health(_request: web.Request):
    return web.Response(text="OK")

async def run_http_server():
    app = web.Application()
    app.router.add_get("/", _health)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"HTTP health server on 0.0.0.0:{port}")

# ========= БД =========
DB_PATH = "closet.db"
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
cursor = db.cursor()
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS clothes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        category TEXT,
        last_worn TEXT,
        last_washed TEXT,
        worn_count INTEGER
    )
    """
)
db.commit()

# ========= FSM =========
class AddClothes(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()

class WearFlow(StatesGroup):
    choosing = State()

class WashFlow(StatesGroup):
    choosing = State()

# ========= Утилиты =========
def build_keyboard_from_items(names: list[str], per_row: int = 2) -> ReplyKeyboardMarkup:
    rows, row = [], []
    for i, name in enumerate(names, 1):
        row.append(KeyboardButton(text=name))
        if i % per_row == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def fetch_user_items(user_id: int) -> list[sqlite3.Row]:
    cursor.execute("SELECT id, name FROM clothes WHERE user_id = ? ORDER BY name", (user_id,))
    return cursor.fetchall()

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

# ========= Команды =========
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Вот что я умею:\n"
        "• /add — добавить вещь\n"
        "• /wear — отметить, что носил вещь\n"
        "• /wash — отметить, что постирал вещь\n"
        "• /status — статус по вещам\n"
    )

# --- /add ---
@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddClothes.waiting_for_name)
    await message.answer("Введи название вещи:")

@router.message(AddClothes.waiting_for_name, F.text.len() > 0)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddClothes.waiting_for_category)
    await message.answer("Укажи категорию (футболка, джинсы и т.п.):")

@router.message(AddClothes.waiting_for_category, F.text.len() > 0)
async def process_category(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"].strip()
    category = message.text.strip()
    cursor.execute(
        """
        INSERT INTO clothes (user_id, name, category, last_worn, last_washed, worn_count)
        VALUES (?, ?, ?, NULL, NULL, 0)
        """,
        (message.from_user.id, name, category),
    )
    db.commit()
    await state.clear()
    await message.answer(f"✅ Добавлена вещь: <b>{name}</b> ({category})")

# --- /wear (носил) ---
@router.message(Command("wear"))
async def cmd_wear(message: Message, state: FSMContext):
    items = fetch_user_items(message.from_user.id)
    if not items:
        await message.answer("Пока нет вещей. Добавь через /add")
        return
    kb = build_keyboard_from_items([row["name"] for row in items])
    await state.set_state(WearFlow.choosing)
    await message.answer("Выбери, что носил:", reply_markup=kb)

@router.message(WearFlow.choosing, F.text)
async def mark_worn(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    cursor.execute(
        "SELECT id FROM clothes WHERE user_id = ? AND name = ?",
        (message.from_user.id, message.text),
    )
    row = cursor.fetchone()
    if not row:
        await message.answer("Нет такой вещи. Выбери из клавиатуры или «Отмена».")
        return
    cursor.execute(
        "UPDATE clothes SET last_worn = ?, worn_count = worn_count + 1 WHERE id = ?",
        (now_iso(), row["id"]),
    )
    db.commit()
    await state.clear()
    await message.answer(f"🧥 Отмечено: носил «{message.text}».", reply_markup=ReplyKeyboardRemove())

# --- /wash (постирал) ---
@router.message(Command("wash"))
async def cmd_wash(message: Message, state: FSMContext):
    items = fetch_user_items(message.from_user.id)
    if not items:
        await message.answer("Пока нет вещей. Добавь через /add")
        return
    kb = build_keyboard_from_items([row["name"] for row in items])
    await state.set_state(WashFlow.choosing)
    await message.answer("Что ты постирал?", reply_markup=kb)

@router.message(WashFlow.choosing, F.text)
async def mark_washed(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    cursor.execute(
        "SELECT id FROM clothes WHERE user_id = ? AND name = ?",
        (message.from_user.id, message.text),
    )
    row = cursor.fetchone()
    if not row:
        await message.answer("Нет такой вещи. Выбери из клавиатуры или «Отмена».")
        return
    cursor.execute(
        "UPDATE clothes SET last_washed = ?, worn_count = 0 WHERE id = ?",
        (now_iso(), row["id"]),
    )
    db.commit()
    await state.clear()
    await message.answer(f"🧼 «{message.text}» отмечена как чистая.", reply_markup=ReplyKeyboardRemove())

# --- /status ---
@router.message(Command("status"))
async def cmd_status(message: Message):
    cursor.execute(
        """
        SELECT name, last_worn, last_washed, worn_count
        FROM clothes
        WHERE user_id = ?
        ORDER BY name
        """,
        (message.from_user.id,),
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Пока нет вещей. Добавь через /add")
        return
    lines = []
    for r in rows:
        name = r["name"]
        worn = r["last_worn"] or "никогда"
        washed = r["last_washed"] or "никогда"
        count = r["worn_count"] or 0
        line = (
            f"👕 <b>{name}</b>\n"
            f"  • Надевалось: {count} раз\n"
            f"  • Последний раз носил: {worn}\n"
            f"  • Последняя стирка: {washed}"
        )
        if count >= 3:
            line += "\n  ❗ Пора постирать!"
        lines.append(line)
    await message.answer("\n\n".join(lines))

# ========= Точка входа =========
async def main():
    asyncio.create_task(run_http_server())
    dp.include_router(router)
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
        handle_signals=False,
    )

if __name__ == "__main__":
    asyncio.run(main())
