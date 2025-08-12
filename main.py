# main.py
from dotenv import load_dotenv
load_dotenv()

import os
import re
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, BotCommand
)

# ======================= ENV / DEFAULTS =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))  # дефолт – 09:00
DEFAULT_TZ_OFFSET = float(os.getenv("TZ_OFFSET", "3"))        # дефолт – Москва (UTC+3)

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),  # без DeprecationWarning
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# ======================= Keep-alive HTTP (Render Web Service) =======================
async def handle_root(_):
    return web.Response(text="OK")

async def start_web_app():
    app = web.Application()
    app.add_routes([web.get("/"), web.get("/healthz")], handler=handle_root)  # type: ignore
    # совместимая запись для старых/новых версий aiohttp:
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_root)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"HTTP keep-alive on 0.0.0.0:{port}")

# ======================= DB =======================
db = sqlite3.connect("closet.db", check_same_thread=False)
db.row_factory = sqlite3.Row
cur = db.cursor()

cur.execute("""
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

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    notify_enabled INTEGER DEFAULT 1
)
""")
db.commit()

def _ensure_column(table: str, column: str, alter_sql: str) -> None:
    cur.execute(f"PRAGMA table_info({table})")
    cols = {row["name"] for row in cur.fetchall()}
    if column not in cols:
        logging.info(f"Adding column {table}.{column}")
        cur.execute(alter_sql)
        db.commit()

# Мягкие миграции под настройки пользователя
_ensure_column("users", "notify_hour", "ALTER TABLE users ADD COLUMN notify_hour INTEGER")
_ensure_column("users", "notify_minute", "ALTER TABLE users ADD COLUMN notify_minute INTEGER")
_ensure_column("users", "tz_offset", "ALTER TABLE users ADD COLUMN tz_offset REAL")
_ensure_column("users", "last_reminder_date", "ALTER TABLE users ADD COLUMN last_reminder_date TEXT")

# ======================= FSM =======================
class AddClothes(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()

class WearFlow(StatesGroup):
    choosing = State()

class WashFlow(StatesGroup):
    choosing = State()

# ======================= Utils =======================
def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск и помощь"),
        BotCommand(command="add", description="Добавить вещь"),
        BotCommand(command="wear", description="Отметить: надел"),
        BotCommand(command="wash", description="Отметить: постирал"),
        BotCommand(command="status", description="Показать статус вещей"),
        BotCommand(command="notify_on", description="Включить напоминания"),
        BotCommand(command="notify_off", description="Выключить напоминания"),
        BotCommand(command="notify_time", description="Время напоминания (HH[:MM])"),
        BotCommand(command="notify_tz", description="Часовой пояс UTC±смещение"),
    ])

def upsert_user(user_id: int):
    cur.execute("""
        INSERT INTO users(user_id, notify_enabled, notify_hour, notify_minute, tz_offset)
        VALUES (?, 1, ?, 0, ?)
        ON CONFLICT(user_id) DO NOTHING
    """, (user_id, DEFAULT_REMINDER_HOUR, DEFAULT_TZ_OFFSET))
    db.commit()

def get_user_settings(user_id: int):
    upsert_user(user_id)
    cur.execute("""
        SELECT notify_enabled,
               COALESCE(notify_hour, ?)   AS notify_hour,
               COALESCE(notify_minute, 0) AS notify_minute,
               COALESCE(tz_offset, ?)     AS tz_offset,
               last_reminder_date
        FROM users WHERE user_id=?
    """, (DEFAULT_REMINDER_HOUR, DEFAULT_TZ_OFFSET, user_id))
    return cur.fetchone()

def set_notify(user_id: int, enabled: bool):
    upsert_user(user_id)
    cur.execute("UPDATE users SET notify_enabled=? WHERE user_id=?", (1 if enabled else 0, user_id))
    db.commit()

def set_notify_time(user_id: int, hour: int, minute: int):
    upsert_user(user_id)
    cur.execute("UPDATE users SET notify_hour=?, notify_minute=? WHERE user_id=?", (hour, minute, user_id))
    db.commit()

def set_tz(user_id: int, tz_offset: float):
    upsert_user(user_id)
    cur.execute("UPDATE users SET tz_offset=? WHERE user_id=?", (tz_offset, user_id))
    db.commit()

def set_last_sent(user_id: int, date_str: str):
    cur.execute("UPDATE users SET last_reminder_date=? WHERE user_id=?", (date_str, user_id))
    db.commit()

def build_keyboard(names: List[str], per_row: int = 3) -> ReplyKeyboardMarkup:
    rows, row = [], []
    for i, n in enumerate(names, 1):
        row.append(KeyboardButton(text=n))
        if i % per_row == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# ======================= Handlers =======================
@router.message(F.text == "/start")
async def cmd_start(message: Message):
    upsert_user(message.from_user.id)
    s = get_user_settings(message.from_user.id)
    await message.answer(
        "Привет! Я помогу отслеживать одежду и напоминать о стирке.\n\n"
        "Команды:\n"
        "• /add — добавить вещь\n"
        "• /wear — отметить, что надел\n"
        "• /wash — отметить, что постирал\n"
        "• /status — статус вещей\n"
        "• /notify_on — включить напоминания\n"
        "• /notify_off — выключить напоминания\n"
        "• /notify_time HH[:MM] — время напоминаний (сейчас "
        f"{s['notify_hour']:02d}:{s['notify_minute']:02d})\n"
        "• /notify_tz ±X[.5] — часовой пояс (сейчас UTC"
        f"{float(s['tz_offset']):+g})\n"
    )

@router.message(F.text == "/notify_on")
async def notify_on(message: Message):
    set_notify(message.from_user.id, True)
    await message.answer("Ежедневные напоминания включены. ✉️")

@router.message(F.text == "/notify_off")
async def notify_off(message: Message):
    set_notify(message.from_user.id, False)
    await message.answer("Ежедневные напоминания выключены. 🔕")

@router.message(F.text.regexp(r"^/notify_time(\s+.+)?$"))
async def notify_time(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        s = get_user_settings(message.from_user.id)
        await message.answer(
            f"Текущее время: {s['notify_hour']:02d}:{s['notify_minute']:02d}\n"
            "Задай так: /notify_time 9 или /notify_time 09:30"
        )
        return
    val = parts[1].strip()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?$", val)
    if not m:
        await message.answer("Неверный формат. Пример: /notify_time 9 или /notify_time 09:30")
        return
    hour = int(m.group(1)); minute = int(m.group(2) or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await message.answer("Часы 0–23, минуты 0–59.")
        return
    set_notify_time(message.from_user.id, hour, minute)
    await message.answer(f"Ок! Буду напоминать в {hour:02d}:{minute:02d} по твоему часовому поясу.")

@router.message(F.text.regexp(r"^/notify_tz(\s+.+)?$"))
async def notify_tz(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        s = get_user_settings(message.from_user.id)
        await message.answer(
            f"Текущий часовой пояс: UTC{float(s['tz_offset']):+g}\n"
            "Задай так: /notify_tz +3 или /notify_tz -5 или /notify_tz +5.5"
        )
        return
    val = parts[1].strip().replace(",", ".")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)", val)
    if not m:
        await message.answer("Неверный формат. Пример: /notify_tz +3 или /notify_tz -5 или /notify_tz +5.5")
        return
    tz = float(m.group(1))
    if not (-12.0 <= tz <= 14.0):
        await message.answer("Смещение допустимо от -12 до +14.")
        return
    set_tz(message.from_user.id, tz)
    await message.answer(f"Часовой пояс установлен: UTC{tz:+g}")

# ---- Добавление вещи ----
@router.message(F.text == "/add")
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddClothes.waiting_for_name)
    await message.answer("Введи название вещи:")

@router.message(AddClothes.waiting_for_name, F.text.len() > 0)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddClothes.waiting_for_category)
    await message.answer("Укажи категорию (футболка, джинсы и т.п.):")

@router.message(AddClothes.waiting_for_category, F.text.len() > 0)
async def add_cat(message: Message, state: FSMContext):
    data = await state.get_data()
    cur.execute("""
        INSERT INTO clothes (user_id, name, category, last_worn, last_washed, worn_count)
        VALUES (?, ?, ?, NULL, NULL, 0)
    """, (message.from_user.id, data["name"], message.text.strip()))
    db.commit()
    await state.clear()
    await message.answer(f"✅ Добавил: <b>{data['name']}</b> ({message.text.strip()})")

# ---- Носил вещь (/wear) ----
@router.message(F.text == "/wear")
async def cmd_wear(message: Message, state: FSMContext):
    cur.execute("SELECT name FROM clothes WHERE user_id=? ORDER BY name", (message.from_user.id,))
    names = [r["name"] for r in cur.fetchall()]
    if not names:
        await message.answer("Пока нет вещей. Добавь через /add")
        return
    await state.set_state(WearFlow.choosing)
    await message.answer("Что ты <b>надел</b>?", reply_markup=build_keyboard(names))

@router.message(WearFlow.choosing, F.text)
async def wear_choose(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    cur.execute("SELECT id FROM clothes WHERE user_id=? AND name=?", (message.from_user.id, message.text))
    row = cur.fetchone()
    if not row:
        await message.answer("Не нашёл такую вещь. Выбери из клавиатуры или «Отмена».")
        return
    cur.execute("UPDATE clothes SET last_worn=?, worn_count=worn_count+1 WHERE id=?", (now_iso(), row["id"]))
    db.commit()
    await state.clear()
    await message.answer(f"👕 Отмечено: носил «{message.text}».", reply_markup=ReplyKeyboardRemove())

# ---- Постирал вещь (/wash) ----
@router.message(F.text == "/wash")
async def cmd_wash(message: Message, state: FSMContext):
    cur.execute("SELECT name FROM clothes WHERE user_id=? ORDER BY name", (message.from_user.id,))
    names = [r["name"] for r in cur.fetchall()]
    if not names:
        await message.answer("Пока нет вещей. Добавь через /add")
        return
    await state.set_state(WashFlow.choosing)
    await message.answer("Что ты <b>постирал</b>?", reply_markup=build_keyboard(names))

@router.message(WashFlow.choosing, F.text)
async def wash_choose(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    cur.execute("SELECT id FROM clothes WHERE user_id=? AND name=?", (message.from_user.id, message.text))
    row = cur.fetchone()
    if not row:
        await message.answer("Не нашёл такую вещь. Выбери из клавиатуры или «Отмена».")
        return
    cur.execute("UPDATE clothes SET last_washed=?, worn_count=0 WHERE id=?", (now_iso(), row["id"]))
    db.commit()
    await state.clear()
    await message.answer(f"🧼 «{message.text}» отмечена как чистая!", reply_markup=ReplyKeyboardRemove())

# ---- Статус ----
@router.message(F.text == "/status")
async def cmd_status(message: Message):
    cur.execute("""
        SELECT name, last_worn, last_washed, worn_count
        FROM clothes
        WHERE user_id=?
        ORDER BY name COLLATE NOCASE
    """, (message.from_user.id,))
    rows = cur.fetchall()
    if not rows:
        await message.answer("Нет вещей. Добавь через /add")
        return
    lines = []
    now = datetime.utcnow()
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
        dt_worn = parse_iso(r["last_worn"])
        dt_washed = parse_iso(r["last_washed"])
        if dt_worn and (dt_washed is None or dt_washed < dt_worn):
            if now - dt_worn >= timedelta(days=7):
                line += "\n  ❗ Пора постирать!"
        lines.append(line)
    await message.answer("\n\n".join(lines))

# ======================= Reminder logic =======================
def due_items_for_user(user_id: int) -> Tuple[List[str], List[str]]:
    cur.execute("SELECT name, last_worn, last_washed FROM clothes WHERE user_id=?", (user_id,))
    rows = cur.fetchall()
    need_wash, long_idle = [], []
    now = datetime.utcnow()
    for r in rows:
        name = r["name"]
        dt_worn = parse_iso(r["last_worn"])
        dt_washed = parse_iso(r["last_washed"])
        # 1) носил и не стирал >= 7 дней
        if dt_worn and (dt_washed is None or dt_washed < dt_worn):
            if now - dt_worn >= timedelta(days=7):
                need_wash.append(name)
        # 2) чистая (стирка свежее ношения/ношения нет) и 30+ дней
        if dt_washed and (dt_worn is None or dt_washed >= dt_worn):
            if now - dt_washed >= timedelta(days=30):
                long_idle.append(name)
    return need_wash, long_idle

async def send_one_user_reminder(uid: int):
    need_wash, long_idle = due_items_for_user(uid)
    if not need_wash and not long_idle:
        return
    parts = []
    if need_wash:
        parts.append("🧺 Пора постирать:\n" + "\n".join(f"• {n}" for n in need_wash))
    if long_idle:
        parts.append("🧷 Давно лежат чистыми (30+ дней):\n" + "\n".join(f"• {n}" for n in long_idle))
    text = "Ежедневное напоминание:\n\n" + "\n\n".join(parts)
    try:
        await bot.send_message(uid, text)
    except Exception as e:
        logging.warning(f"Failed to send reminder to {uid}: {e}")

async def reminder_loop():
    """
    Каждые 30 сек проверяем, не пора ли отправить напоминание
    каждому пользователю согласно его notify_time и tz_offset.
    Шлём не чаще 1 раза в сутки (фиксируем last_reminder_date).
    """
    while True:
        now_utc = datetime.utcnow()
        cur.execute("SELECT * FROM users WHERE notify_enabled=1")
        users = cur.fetchall()
        for s in users:
            uid = s["user_id"]
            hour = int(s["notify_hour"] if s["notify_hour"] is not None else DEFAULT_REMINDER_HOUR)
            minute = int(s["notify_minute"] if s["notify_minute"] is not None else 0)
            tz = float(s["tz_offset"] if s["tz_offset"] is not None else DEFAULT_TZ_OFFSET)
            local_now = now_utc + timedelta(hours=tz)
            local_date = local_now.date().isoformat()
            if s["last_reminder_date"] == local_date:
                continue
            if local_now.hour == hour and local_now.minute == minute:
                logging.info(f"Send reminder to {uid} at {hour:02d}:{minute:02d} (UTC{tz:+g})")
                await send_one_user_reminder(uid)
                set_last_sent(uid, local_date)
        await asyncio.sleep(30)

# ======================= MAIN =======================
async def main():
    dp.include_router(router)
    await set_commands()
    await start_web_app()             # держим открытый порт для Render Web Service
    asyncio.create_task(reminder_loop())  # запускаем планировщик
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
