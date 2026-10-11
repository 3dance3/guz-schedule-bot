"""Телеграм-бот с расписанием ГУЗ (ruz.guz.ru). Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from ruz_client import GROUP, TEACHER, Entity, Lesson, RuzClient, RuzError

log = logging.getLogger("schedule-bot")

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "➡️ Завтра"
BTN_WEEK = "🗓 Неделя"
BTN_NEXT = "⏭ След. неделя"
BTN_CHANGE = "🔄 Сменить"

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_NEXT)],
        [KeyboardButton(text=BTN_CHANGE)],
    ],
    resize_keyboard=True,
)

esc = html.escape


# ---------- хранилище выбора пользователей ----------
class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'group',
                kind TEXT, stype TEXT, eid TEXT, label TEXT)"""
        )
        self.conn.commit()

    def get(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def set_mode(self, user_id: int, mode: str) -> None:
        self.conn.execute(
            "INSERT INTO users(user_id, mode) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode",
            (user_id, mode),
        )
        self.conn.commit()

    def set_selection(self, user_id: int, ent: Entity) -> None:
        self.conn.execute(
            "INSERT INTO users(user_id, kind, stype, eid, label) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET kind=excluded.kind, stype=excluded.stype, "
            "eid=excluded.eid, label=excluded.label",
            (user_id, ent.kind, ent.stype, ent.eid, ent.label),
        )
        self.conn.commit()


# ---------- форматирование ----------
def format_blocks(lessons: list[Lesson], show_teacher: bool, own_label: str = "") -> list[str]:
    by_day: dict[date, list[Lesson]] = {}
    for l in lessons:
        by_day.setdefault(l.day, []).append(l)

    blocks = []
    for day in sorted(by_day):
        lines = [f"📅 <b>{WEEKDAYS[day.weekday()]}, {day:%d.%m}</b>"]
        for l in by_day[day]:
            time = f"{l.begin}–{l.end}" if l.begin and l.end else (l.begin or "")
            head = f"🕘 {esc(time)}  " if time else ""
            title = f"<b>{esc(l.title or 'Занятие')}</b>"
            kind = f" ({esc(l.kind)})" if l.kind else ""
            lines.append(f"\n{head}{title}{kind}")
            place = ", ".join(p for p in (l.room, l.building) if p)
            if place:
                lines.append(f"📍 {esc(place)}")
            if show_teacher and l.teacher:
                lines.append(f"👤 {esc(l.teacher)}")
            if l.group:
                # В расписании группы показываем только подгруппу (если она отличается от самой группы),
                # в расписании преподавателя всегда показываем, для какой группы пара.
                if not show_teacher or l.group.casefold() != own_label.casefold():
                    lines.append(f"👥 {esc(l.group)}")
        blocks.append("\n".join(lines))
    return blocks


def split_message(header: str, blocks: list[str], limit: int = 3800) -> list[str]:
    chunks, current = [], header
    for block in blocks:
        if len(current) + len(block) + 2 > limit and current != header:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


# ---------- бот ----------
def build_dispatcher(ruz: RuzClient, store: Store) -> Dispatcher:
    dp = Dispatcher()
    pending: dict[int, list[Entity]] = {}

    def today() -> date:
        return datetime.now(TZ).date()

    def week_bounds(d: date) -> tuple[date, date]:
        monday = d - timedelta(days=d.weekday())
        return monday, monday + timedelta(days=6)

    async def show(message: Message, start: date, end: date, title: str) -> None:
        row = store.get(message.from_user.id)
        if not row or not row["eid"]:
            await message.answer(
                "Сначала выбери группу: просто напиши её название, например <code>Г.ГК.24.Б2</code>.\n"
                "Для расписания преподавателя сначала отправь /teacher.",
                reply_markup=KEYBOARD,
            )
            return
        ent = Entity(row["kind"], row["stype"], row["eid"], row["label"])
        try:
            lessons = await ruz.schedule(ent, start, end)
        except RuzError as e:
            await message.answer(f"⚠️ {esc(str(e))}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await message.answer("⚠️ Сайт расписания сейчас не отвечает. Попробуй чуть позже.")
            return
        except Exception:
            log.exception("schedule failed")
            await message.answer("⚠️ Что-то пошло не так. Попробуй ещё раз.")
            return

        header = f"<b>{esc(ent.label)}</b> — {title}"
        if not lessons:
            await message.answer(f"{header}\n\nПар нет 🎉", reply_markup=KEYBOARD)
            return
        blocks = format_blocks(lessons, show_teacher=ent.kind == GROUP, own_label=ent.label)
        for chunk in split_message(header, blocks):
            await message.answer(chunk, reply_markup=KEYBOARD)

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        store.set_mode(message.from_user.id, GROUP)
        await message.answer(
            "Привет! Я показываю расписание ГУЗ.\n\n"
            "Напиши название своей группы, например <code>Г.ГК.24.Б2</code>, "
            "и я её запомню. Дальше пользуйся кнопками внизу.\n\n"
            "Если ты преподаватель, отправь /teacher и затем фамилию.",
            reply_markup=KEYBOARD,
        )

    @dp.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(
            "/group — выбрать группу\n/teacher — выбрать преподавателя\n"
            "Кнопки внизу показывают расписание на сегодня, завтра и неделю.",
            reply_markup=KEYBOARD,
        )

    @dp.message(Command("group"))
    @dp.message(F.text == BTN_CHANGE)
    async def ask_group(message: Message) -> None:
        store.set_mode(message.from_user.id, GROUP)
        await message.answer(
            "Напиши название группы, например <code>Г.ГК.24.Б2</code>.\n"
            "Для преподавателя отправь /teacher.",
            reply_markup=KEYBOARD,
        )

    @dp.message(Command("teacher"))
    async def ask_teacher(message: Message) -> None:
        store.set_mode(message.from_user.id, TEACHER)
        await message.answer("Напиши фамилию преподавателя, например <code>Костеша</code>.", reply_markup=KEYBOARD)

    @dp.message(F.text == BTN_TODAY)
    async def btn_today(message: Message) -> None:
        d = today()
        await show(message, d, d, f"сегодня, {d:%d.%m}")

    @dp.message(F.text == BTN_TOMORROW)
    async def btn_tomorrow(message: Message) -> None:
        d = today() + timedelta(days=1)
        await show(message, d, d, f"завтра, {d:%d.%m}")

    @dp.message(F.text == BTN_WEEK)
    async def btn_week(message: Message) -> None:
        start_d, end_d = week_bounds(today())
        await show(message, start_d, end_d, f"неделя {start_d:%d.%m}–{end_d:%d.%m}")

    @dp.message(F.text == BTN_NEXT)
    async def btn_next(message: Message) -> None:
        start_d, end_d = week_bounds(today() + timedelta(days=7))
        await show(message, start_d, end_d, f"неделя {start_d:%d.%m}–{end_d:%d.%m}")

    async def select(message: Message, user_id: int, ent: Entity) -> None:
        store.set_selection(user_id, ent)
        await message.answer(
            f"Запомнил: <b>{esc(ent.label)}</b> ✅\nТеперь жми кнопки внизу.",
            reply_markup=KEYBOARD,
        )

    @dp.callback_query(F.data.startswith("pick:"))
    async def on_pick(call: CallbackQuery) -> None:
        options = pending.get(call.from_user.id)
        try:
            ent = options[int(call.data.split(":", 1)[1])]
        except (TypeError, ValueError, IndexError):
            await call.answer("Список устарел, повтори поиск.", show_alert=True)
            return
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await select(call.message, call.from_user.id, ent)

    @dp.message(F.text & ~F.text.startswith("/"))
    async def search(message: Message) -> None:
        user_id = message.from_user.id
        row = store.get(user_id)
        mode = row["mode"] if row else GROUP
        try:
            found = await ruz.search(mode, message.text)
        except RuzError as e:
            await message.answer(f"⚠️ {esc(str(e))}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await message.answer("⚠️ Сайт расписания сейчас не отвечает. Попробуй чуть позже.")
            return
        except Exception:
            log.exception("search failed")
            await message.answer("⚠️ Что-то пошло не так. Попробуй ещё раз.")
            return

        if not found:
            hint = "группу" if mode == GROUP else "преподавателя"
            await message.answer(f"Не нашёл такую {hint}. Проверь написание и попробуй ещё раз.")
            return

        exact = [e for e in found if e.label.casefold() == message.text.strip().casefold()]
        if len(exact) == 1 or len(found) == 1:
            await select(message, user_id, (exact or found)[0])
            return

        found = found[:8]
        pending[user_id] = found
        buttons = [
            [InlineKeyboardButton(
                text=(f"{e.label} ({e.description})" if e.description else e.label)[:60],
                callback_data=f"pick:{i}",
            )]
            for i, e in enumerate(found)
        ]
        await message.answer("Нашёл несколько вариантов, выбери нужный:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Хостинги передают токен под разными именами переменной, поддерживаем самые частые.
    token = next(
        (os.environ[k] for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TOKEN", "API_TOKEN") if os.environ.get(k)),
        None,
    )
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Получи токен у @BotFather и задай переменную окружения (см. README).")

    base = os.environ.get("RUZ_BASE", "https://ruz.guz.ru")
    store = Store(os.environ.get("DB_PATH", "users.db"))
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    async with aiohttp.ClientSession(headers={"User-Agent": "guz-schedule-bot"}) as session:
        dp = build_dispatcher(RuzClient(session, base), store)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
