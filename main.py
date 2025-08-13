import asyncio
import logging
import os
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from typing import List, Optional

from aiohttp import web
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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
# Настройки / инициализация
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("closet-bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# =========================
# БД (SQLite)
# =========================
DB_PATH = "closet.db"
db = sqlite3.connect(DB_PATH)
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

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    notify_on INTEGER DEFAULT 0,           -- 0/1
    notify_time TEXT DEFAULT '09:00',      -- HH:MM
    tz TEXT DEFAULT 'Europe/Moscow'        -- IANA TZ
)
"""
)
db.commit()

# ==========
# FSM (для добавления)
# ==========
class AddClothes(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()

class ChangeNotifyTime(StatesGroup):
    waiting_for_time = State()

class ChangeTimezone(StatesGroup):
    waiting_for_tz = State()

# =========================
# Память (режим выбора)
# =========================
# user_id -> "wear" | "wash"
_pending_action: dict[int, str] = {}

# =========================
# Утилиты
# =========================
def now_tz(tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(tz)

def get_or_create_user_settings(user_id: int) -> sqlite3.Row:
    cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO user_settings (user_id, notify_on, notify_time, tz) VALUES (?, 0, '09:00', 'Europe/Moscow')",
            (user_id,),
        )
        db.commit()
        cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row

def parse_hhmm(text: str) -> Optional[str]:
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    h, m = parts
    if not (h.isdigit() and m.isdigit()):
        return None
    hh, mm = int(h), int(m)
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None

def list_user_items(user_id: int) -> List[str]:
    cursor.execute("SELECT name FROM clothes WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,))
    return [row["name"] for row in cursor.fetchall()]

def chunk_buttons(names: List[str], per_row: int = 3) -> List[List[KeyboardButton]]:
    rows = []
    row = []
    for nm in names:
        row.append(KeyboardButton(text=nm))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows

def human_date(iso: Optional[str]) -> str:
    if not iso:
        return "никогда"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso

# =========================
# Команды (меню)
# =========================
async def set_commands():
    cmds = [
        BotCommand(command="start", description="Начало / меню"),
        BotCommand(command="add", description="Добавить вещь"),
        BotCommand(command="wear", description="Отметить: носил"),
        BotCommand(command="wash", description="Отметить: постирал"),
        BotCommand(command="status", description="Статус вещей"),
        BotCommand(command="notify_on", description="Включить напоминания"),
        BotCommand(command="notify_off", description="Выключить напоминания"),
        BotCommand(command="notify_time", description="Время напоминания (HH:MM)"),
        BotCommand(command="notify_tz", description="Часовой пояс (IANA)"),
        BotCommand(command="help", description="Справка"),
    ]
    await bot.set_my_commands(cmds)

# =========================
# Хэндлеры
# =========================
@router.message(F.text.in_({"/start", "/help"}))
async def cmd_start(message: Message):
    s = get_or_create_user_settings(message.from_user.id)
    text = (
        "Привет! Я помогу отслеживать гардероб и напомню, когда пора стирать 👕\n\n"
        "Команды:\n"
        "• /add — добавить вещь\n"
        "• /wear — отметить, что носил\n"
        "• /wash — отметить, что постирал\n"
        "• /status — текущий статус\n\n"
        "Напоминания:\n"
        "• /notify_on — включить, /notify_off — выключить\n"
        "• /notify_time — время (HH:MM)\n"
        "• /notify_tz — часовой пояс (например Europe/Moscow)\n\n"
        f"Сейчас: уведомления <b>{'включены' if s['notify_on'] else 'выключены'}</b>, "
        f"время <b>{s['notify_time']}</b>, TZ <b>{s['tz']}</b>."
    )
    await message.answer(text)

@router.message(F.text == "/add")
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddClothes.waiting_for_name)
    await message.answer("Введи название вещи:", reply_markup=ReplyKeyboardRemove())

@router.message(AddClothes.waiting_for_name)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddClothes.waiting_for_category)
    await message.answer("Укажи категорию (например: футболка, джинсы, кроссовки):")

@router.message(AddClothes.waiting_for_category)
async def add_category(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("name").strip()
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
    await message.answer(f"Добавлено: <b>{name}</b> ({category})")

@router.message(F.text == "/status")
async def cmd_status(message: Message):
    cursor.execute(
        "SELECT name, last_worn, last_washed, worn_count FROM clothes WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (message.from_user.id,),
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет вещей. Используй /add")
        return
    lines = []
    for row in rows:
        name = row["name"]
        worn = human_date(row["last_worn"])
        washed = human_date(row["last_washed"])
        count = row["worn_count"]
        line = (
            f"👕 <b>{name}</b>\n"
            f"  — Надевалось: {count} раз\n"
            f"  — Последний раз носил: {worn}\n"
            f"  — Последняя стирка: {washed}"
        )
        if count >= 3:
            line += "\n  ❗ Похоже, стоит постирать 🙂"
        lines.append(line)
    await message.answer("\n\n".join(lines))

# ----- wear / wash упрощённая логика -----
@router.message(F.text == "/wear")
async def cmd_wear(message: Message):
    items = list_user_items(message.from_user.id)
    if not items:
        await message.answer("Нет добавленных вещей. Используй /add")
        return
    _pending_action[message.from_user.id] = "wear"
    kb = ReplyKeyboardMarkup(keyboard=chunk_buttons(items, 3), resize_keyboard=True)
    await message.answer("Выбери вещь, которую ты <b>носил</b>:", reply_markup=kb)

@router.message(F.text == "/wash")
async def cmd_wash(message: Message):
    items = list_user_items(message.from_user.id)
    if not items:
        await message.answer("Нет добавленных вещей. Используй /add")
        return
    _pending_action[message.from_user.id] = "wash"
    kb = ReplyKeyboardMarkup(keyboard=chunk_buttons(items, 3), resize_keyboard=True)
    await message.answer("Выбери вещь, которую ты <b>постирал</b>:", reply_markup=kb)

@router.message(F.text)
async def handle_item_click(message: Message):
    """Если пользователь в режиме wear/wash и нажал название вещи — применяем действие."""
    user_id = message.from_user.id
    action = _pending_action.get(user_id)
    if not action:
        return  # не ждём выбора — игнорируем

    name = message.text.strip()
    cursor.execute("SELECT id FROM clothes WHERE user_id = ? AND name = ?", (user_id, name))
    row = cursor.fetchone()
    if not row:
        return  # не наша кнопка

    now_iso = datetime.now().isoformat(timespec="minutes")
    if action == "wear":
        cursor.execute(
            "UPDATE clothes SET last_worn = ?, worn_count = worn_count + 1 WHERE user_id = ? AND name = ?",
            (now_iso, user_id, name),
        )
        db.commit()
        await message.answer(
            f"Отмечено: ты носил «{name}» сегодня.",
            reply_markup=ReplyKeyboardRemove()
        )
    elif action == "wash":
        cursor.execute(
            "UPDATE clothes SET last_washed = ?, worn_count = 0 WHERE user_id = ? AND name = ?",
            (now_iso, user_id, name),
        )
        db.commit()
        await message.answer(
            f"Отмечено: «{name}» постирана!",
            reply_markup=ReplyKeyboardRemove()
        )

    # очистим режим
    _pending_action.pop(user_id, None)

# ----- уведомления -----
@router.message(F.text.in_({"/notify_on", "/notify_off"}))
async def toggle_notify(message: Message):
    on = 1 if message.text == "/notify_on" else 0
    cursor.execute("UPDATE user_settings SET notify_on = ? WHERE user_id = ?", (on, message.from_user.id))
    db.commit()
    s = get_or_create_user_settings(message.from_user.id)
    await message.answer(
        f"Уведомления <b>{'включены' if s['notify_on'] else 'выключены'}</b>. "
        f"Время: <b>{s['notify_time']}</b>, TZ: <b>{s['tz']}</b>"
    )

@router.message(F.text == "/notify_time")
async def ask_notify_time(message: Message, state: FSMContext):
    await state.set_state(ChangeNotifyTime.waiting_for_time)
    await message.answer("Введи время в формате HH:MM (например 09:30).", reply_markup=ReplyKeyboardRemove())

@router.message(ChangeNotifyTime.waiting_for_time)
async def set_notify_time(message: Message, state: FSMContext):
    val = parse_hhmm(message.text)
    if not val:
        await message.answer("Неверный формат. Введи HH:MM, например 08:45.")
        return
    cursor.execute("UPDATE user_settings SET notify_time = ? WHERE user_id = ?", (val, message.from_user.id))
    db.commit()
    await state.clear()
    s = get_or_create_user_settings(message.from_user.id)
    await message.answer(f"Готово! Время напоминания: <b>{s['notify_time']}</b>. Текущий TZ: <b>{s['tz']}</b>.")

@router.message(F.text == "/notify_tz")
async def ask_tz(message: Message, state: FSMContext):
    await state.set_state(ChangeTimezone.waiting_for_tz)
    await message.answer(
        "Введи часовой пояс (IANA), например: <code>Europe/Moscow</code>, <code>Europe/Berlin</code>, "
        "<code>Asia/Almaty</code>.\nСписок: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(ChangeTimezone.waiting_for_tz)
async def set_tz(message: Message, state: FSMContext):
    tz_candidate = message.text.strip()
    try:
        _ = ZoneInfo(tz_candidate)
    except Exception:
        await message.answer("Не удалось распознать TZ. Пример: Europe/Moscow. Попробуй ещё раз.")
        return
    cursor.execute("UPDATE user_settings SET tz = ? WHERE user_id = ?", (tz_candidate, message.from_user.id))
    db.commit()
    await state.clear()
    s = get_or_create_user_settings(message.from_user.id)
    await message.answer(f"Готово! TZ: <b>{s['tz']}</b>. Время напоминания: <b>{s['notify_time']}</b>.")

# =========================
# Напоминания
# =========================
REMIND_WORN_NOT_WASHED_DAYS = 7
REMIND_CLEAN_NOT_WORN_DAYS = 30

async def reminders_loop():
    await asyncio.sleep(5)
    sent_guard = {}  # (user_id, 'YYYY-MM-DD HH:MM')

    while True:
        try:
            cursor.execute("SELECT user_id, notify_on, notify_time, tz FROM user_settings WHERE notify_on = 1")
            users = cursor.fetchall()
            for s in users:
                user_id = s["user_id"]
                tz = s["tz"]
                t = s["notify_time"]

                try:
                    now_local = now_tz(tz)
                except Exception:
                    now_local = now_tz("Europe/Moscow")

                hhmm_now = now_local.strftime("%H:%M")
                if hhmm_now != t:
                    continue

                guard_key = (user_id, now_local.strftime("%Y-%m-%d %H:%M"))
                if sent_guard.get(guard_key):
                    continue

                cursor.execute(
                    "SELECT name, last_worn, last_washed FROM clothes WHERE user_id = ? ORDER BY name COLLATE NOCASE",
                    (user_id,),
                )
                rows = cursor.fetchall()
                need_lines = []
                for row in rows:
                    name = row["name"]
                    last_worn = row["last_worn"]
                    last_washed = row["last_washed"]

                    # 1) носил, но не стирал 7 дней
                    if last_worn and (not last_washed or last_washed < last_worn):
                        try:
                            dt_worn = datetime.fromisoformat(last_worn)
                        except Exception:
                            dt_worn = None
                        if dt_worn and datetime.utcnow() >= (dt_worn + timedelta(days=REMIND_WORN_NOT_WASHED_DAYS)):
                            need_lines.append(f"• «{name}»: давно носил — самое время постирать!")

                    # 2) чистая вещь и давно не надевал (30 дней)
                    base = last_washed or last_worn
                    if base:
                        try:
                            dt_base = datetime.fromisoformat(base)
                        except Exception:
                            dt_base = None
                        if dt_base and datetime.utcnow() >= (dt_base + timedelta(days=REMIND_CLEAN_NOT_WORN_DAYS)):
                            need_lines.append(f"• «{name}»: давно не надевал — загляни в шкаф 😉")

                if need_lines:
                    text = "Напоминание 👇\n\n" + "\n".join(need_lines)
                    with suppress(Exception):
                        await bot.send_message(user_id, text)

                sent_guard[guard_key] = True

        except Exception as e:
            log.exception("Ошибка в reminders_loop: %s", e)

        await asyncio.sleep(60)

# =========================
# Keep-alive веб-сервер для Render
# =========================
async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def run_keepalive():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_root)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info(f"HTTP keep-alive started on 0.0.0.0:{port}")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        with suppress(Exception):
            await runner.cleanup()

# =========================
# Главный запуск
# =========================
async def main():
    dp.include_router(router)
    await set_commands()

    keepalive_task = asyncio.create_task(run_keepalive())
    reminders_task = asyncio.create_task(reminders_loop())

    try:
        await dp.start_polling(bot)
    finally:
        for t in (keepalive_task, reminders_task):
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t

if __name__ == "__main__":
    asyncio.run(main())
