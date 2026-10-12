"""Телеграм-бот с расписанием ГУЗ (ruz.guz.ru). Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
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
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

REMIND_MINUTES = 10  # за сколько минут до пары напоминать
MORNING_WINDOW_MIN = 90  # сколько минут после выбранного времени ещё можно прислать утреннее расписание
DEFAULT_MORNING = "07:30"
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
TIME_ARG_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SUBGROUP_RE = re.compile(r"\((\d+)\)\s*$")

BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "➡️ Завтра"
BTN_WEEK = "🗓 Неделя"
BTN_NEXT = "⏭ След. неделя"
BTN_NOTIFY = "🔔 Уведомления"
BTN_CHANGE = "🔄 Сменить"

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_NEXT)],
        [KeyboardButton(text=BTN_NOTIFY), KeyboardButton(text=BTN_CHANGE)],
    ],
    resize_keyboard=True,
)

esc = html.escape


# ---------- хранилище настроек пользователей ----------
class Store:
    FIELDS = {"notify_morning", "notify_pairs", "morning_time", "subgroup", "last_morning"}

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'group',
                kind TEXT, stype TEXT, eid TEXT, label TEXT)"""
        )
        # Новые поля добавляем отдельно, чтобы не ломалась уже существующая база.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for col, ddl in (
            ("notify_morning", "INTEGER NOT NULL DEFAULT 0"),
            ("notify_pairs", "INTEGER NOT NULL DEFAULT 0"),
            ("morning_time", f"TEXT NOT NULL DEFAULT '{DEFAULT_MORNING}'"),
            ("subgroup", "TEXT NOT NULL DEFAULT ''"),
            ("last_morning", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS sent (user_id INTEGER, key TEXT, PRIMARY KEY (user_id, key))"
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
        # При смене группы подгруппа сбрасывается: у новой группы она может быть другой.
        self.conn.execute(
            "INSERT INTO users(user_id, kind, stype, eid, label) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET kind=excluded.kind, stype=excluded.stype, "
            "eid=excluded.eid, label=excluded.label, subgroup=''",
            (user_id, ent.kind, ent.stype, ent.eid, ent.label),
        )
        self.conn.commit()

    def set_fields(self, user_id: int, **fields) -> None:
        bad = set(fields) - self.FIELDS
        if bad:
            raise ValueError(f"unknown fields: {bad}")
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))
        self.conn.commit()

    def notify_users(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE eid IS NOT NULL AND eid != '' "
            "AND (notify_morning=1 OR notify_pairs=1)"
        ).fetchall()

    def was_sent(self, user_id: int, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sent WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone() is not None

    def mark_sent(self, user_id: int, key: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO sent(user_id, key) VALUES(?, ?)", (user_id, key))
        self.conn.commit()

    def cleanup_sent(self, today_iso: str) -> None:
        self.conn.execute("DELETE FROM sent WHERE substr(key, 1, 10) < ?", (today_iso,))
        self.conn.commit()


# ---------- подгруппы и форматирование ----------
def for_subgroup(lessons: list[Lesson], subgroup: str) -> list[Lesson]:
    """Оставляет пары выбранной подгруппы и общие пары (где подгруппа не указана)."""
    if not subgroup:
        return lessons
    result = []
    for l in lessons:
        m = SUBGROUP_RE.search(l.group or "")
        if m and m.group(1) != subgroup:
            continue
        result.append(l)
    return result


def format_lesson(l: Lesson, show_teacher: bool, own_label: str = "", with_time: bool = True) -> list[str]:
    period = f"{l.begin}–{l.end}" if l.begin and l.end else (l.begin or "")
    head = f"🕘 {esc(period)}  " if (with_time and period) else ""
    title = f"<b>{esc(l.title or 'Занятие')}</b>"
    kind = f" ({esc(l.kind)})" if l.kind else ""
    lines = [f"{head}{title}{kind}"]
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
    return lines


def format_blocks(lessons: list[Lesson], show_teacher: bool, own_label: str = "") -> list[str]:
    by_day: dict[date, list[Lesson]] = {}
    for l in lessons:
        by_day.setdefault(l.day, []).append(l)

    blocks = []
    for day in sorted(by_day):
        lines = [f"📅 <b>{WEEKDAYS[day.weekday()]}, {day:%d.%m}</b>"]
        for l in by_day[day]:
            lines.append("")
            lines.extend(format_lesson(l, show_teacher, own_label))
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


def day_label(d: date, base: date) -> str:
    """Понятное название дня: 'сегодня', 'завтра' или день недели с датой."""
    diff = (d - base).days
    name = f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"
    if diff == 0:
        return f"сегодня — {name}"
    if diff == 1:
        return f"завтра — {name}"
    if diff == -1:
        return f"вчера — {name}"
    return name


def day_text(ent: Entity, d: date, base: date, lessons: list[Lesson]) -> str:
    header = f"<b>{esc(ent.label)}</b>\n📅 <b>{day_label(d, base)}</b>"
    if not lessons:
        return f"{header}\n\nПар нет 🎉"
    body = "\n\n".join("\n".join(format_lesson(l, ent.kind == GROUP, ent.label)) for l in lessons)
    return f"{header}\n\n{body}"


def day_nav_markup(d: date, base: date) -> InlineKeyboardMarkup:
    prev_d, next_d = d - timedelta(days=1), d + timedelta(days=1)
    row = [
        InlineKeyboardButton(text=f"◄ {WEEKDAYS_SHORT[prev_d.weekday()]} {prev_d:%d.%m}", callback_data=f"day:{prev_d.isoformat()}"),
        InlineKeyboardButton(text=f"{next_d:%d.%m} {WEEKDAYS_SHORT[next_d.weekday()]} ►", callback_data=f"day:{next_d.isoformat()}"),
    ]
    second = []
    if d != base:
        second.append(InlineKeyboardButton(text="📅 Сегодня", callback_data=f"day:{base.isoformat()}"))
    second.append(InlineKeyboardButton(text="🗓 Неделя", callback_data=f"week:{d.isoformat()}"))
    return InlineKeyboardMarkup(inline_keyboard=[row, second])


# ---------- настройки уведомлений (текст и кнопки) ----------
def _onoff(value) -> str:
    return "ВКЛ ✅" if value else "ВЫКЛ ❌"


def settings_text(row: sqlite3.Row) -> str:
    lines = [
        "🔔 <b>Уведомления</b>",
        f"Расписание: <b>{esc(row['label'] or '')}</b>",
        "",
        f"☀️ Расписание на день утром: <b>{_onoff(row['notify_morning'])}</b>",
        f"    Время: <b>{esc(row['morning_time'])}</b> (изменить: <code>/morning 07:45</code>)",
        f"⏰ Напоминание за {REMIND_MINUTES} минут до пары: <b>{_onoff(row['notify_pairs'])}</b>",
        "    Покажу название пары и аудиторию.",
    ]
    if row["kind"] == GROUP:
        sg = row["subgroup"]
        lines.append(f"👥 Подгруппа: <b>{esc(sg) if sg else 'все'}</b> (влияет и на обычное расписание)")
    lines += ["", "Время указывается по московскому. Всё можно включить и выключить кнопками ниже."]
    return "\n".join(lines)


def settings_markup(row: sqlite3.Row) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"☀️ Утром: {_onoff(row['notify_morning'])}", callback_data="set:morning")],
        [InlineKeyboardButton(
            text=f"⏰ За {REMIND_MINUTES} мин до пары: {_onoff(row['notify_pairs'])}", callback_data="set:pairs"
        )],
    ]
    if row["kind"] == GROUP:
        current = row["subgroup"]
        rows.append([
            InlineKeyboardButton(text=("• " if current == value else "") + label, callback_data=f"set:sg:{value}")
            for value, label in (("", "Все"), ("1", "Подгруппа 1"), ("2", "Подгруппа 2"))
        ])
    rows.append([InlineKeyboardButton(text="🔕 Выключить всё", callback_data="set:off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- рассылка уведомлений ----------
class Notifier:
    """Раз в минуту смотрит, кому пора отправить утреннее расписание или напоминание о паре."""

    def __init__(self, bot: Bot, ruz: RuzClient, store: Store):
        self.bot, self.ruz, self.store = bot, ruz, store
        self.cache: dict[tuple, tuple[float, list[Lesson]]] = {}

    async def _lessons(self, ent: Entity, day: date) -> list[Lesson]:
        now_m = time.monotonic()
        for k in [k for k, (t, _) in self.cache.items() if now_m - t > 3600]:
            del self.cache[k]
        key = (ent.stype, ent.eid, day)
        hit = self.cache.get(key)
        if hit and now_m - hit[0] < 900:
            return hit[1]
        lessons = await self.ruz.schedule(ent, day, day)
        self.cache[key] = (now_m, lessons)
        return lessons

    async def _send(self, user_id: int, text: str) -> None:
        await self.bot.send_message(user_id, text)
        await asyncio.sleep(0.05)  # не упираемся в лимиты Telegram при большой рассылке

    async def handle_user(self, row: sqlite3.Row, now: datetime) -> None:
        uid = row["user_id"]
        today = now.date()
        ent = Entity(row["kind"], row["stype"], row["eid"], row["label"])
        show_teacher = ent.kind == GROUP
        lessons: list[Lesson] | None = None

        async def day_lessons() -> list[Lesson]:
            items = await self._lessons(ent, today)
            return for_subgroup(items, row["subgroup"]) if ent.kind == GROUP else items

        # 1) Утреннее расписание
        if row["notify_morning"] and row["last_morning"] != today.isoformat():
            h, m = (int(x) for x in row["morning_time"].split(":"))
            delta = now.hour * 60 + now.minute - (h * 60 + m)
            if 0 <= delta < MORNING_WINDOW_MIN:
                lessons = await day_lessons()
                if lessons:
                    header = f"☀️ <b>{esc(ent.label)}</b> — сегодня, {today:%d.%m}"
                    for chunk in split_message(header, format_blocks(lessons, show_teacher, ent.label)):
                        await self._send(uid, chunk)
                self.store.set_fields(uid, last_morning=today.isoformat())

        # 2) Напоминание за N минут до пары
        if row["notify_pairs"]:
            if lessons is None:
                lessons = await day_lessons()
            by_start: dict[tuple[int, int], list[Lesson]] = {}
            for l in lessons:
                mt = TIME_RE.search(l.begin or "")
                if mt:
                    by_start.setdefault((int(mt.group(1)), int(mt.group(2))), []).append(l)
            for (h, m), group in sorted(by_start.items()):
                start = datetime.combine(today, dtime(h, m), tzinfo=TZ)
                if start - timedelta(minutes=REMIND_MINUTES) <= now < start:
                    key = f"{today.isoformat()}|{h:02d}:{m:02d}"
                    if self.store.was_sent(uid, key):
                        continue
                    body = "\n\n".join(
                        "\n".join(format_lesson(l, show_teacher, ent.label, with_time=False)) for l in group
                    )
                    text = f"⏰ <b>Через {REMIND_MINUTES} минут пара</b> ({h:02d}:{m:02d})\n\n{body}"
                    await self._send(uid, text)
                    self.store.mark_sent(uid, key)

    async def run_once(self, now: datetime) -> None:
        for row in self.store.notify_users():
            try:
                await self.handle_user(row, now)
            except TelegramForbiddenError:
                # Пользователь заблокировал бота: больше ему не пишем.
                self.store.set_fields(row["user_id"], notify_morning=0, notify_pairs=0)
            except (RuzError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("notify skipped for %s: %s", row["user_id"], e)
            except Exception:
                log.exception("notify failed for %s", row["user_id"])
        if now.strftime("%H:%M") == "03:00":
            self.store.cleanup_sent(now.date().isoformat())

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once(datetime.now(TZ))
            except Exception:
                log.exception("notifier tick failed")
            await asyncio.sleep(60.5 - datetime.now(TZ).second)


# ---------- бот ----------
def build_dispatcher(ruz: RuzClient, store: Store) -> Dispatcher:
    dp = Dispatcher()
    pending: dict[int, list[Entity]] = {}

    def today() -> date:
        return datetime.now(TZ).date()

    def week_bounds(d: date) -> tuple[date, date]:
        monday = d - timedelta(days=d.weekday())
        return monday, monday + timedelta(days=6)

    NO_GROUP = (
        "Сначала выбери группу: просто напиши её название, например <code>Г.ГК.24.Б2</code>.\n"
        "Для расписания преподавателя сначала отправь /teacher."
    )

    def entity_of(row) -> Entity | None:
        if not row or not row["eid"]:
            return None
        return Entity(row["kind"], row["stype"], row["eid"], row["label"])

    async def fetch(ent: Entity, row, start: date, end: date) -> list[Lesson]:
        lessons = await ruz.schedule(ent, start, end)
        if ent.kind == GROUP:
            lessons = for_subgroup(lessons, row["subgroup"])
        return lessons

    async def show_day(message: Message, d: date, edit: bool) -> None:
        """Показывает один день с кнопками ◄ / ► для переключения прямо в сообщении."""
        row = store.get(message.chat.id)
        ent = entity_of(row)
        if ent is None:
            await message.answer(NO_GROUP, reply_markup=KEYBOARD)
            return
        try:
            lessons = await fetch(ent, row, d, d)
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
        text = day_text(ent, d, today(), lessons)
        markup = day_nav_markup(d, today())
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest:
                pass
        else:
            await message.answer(text, reply_markup=markup)

    async def show_range(message: Message, start: date, end: date, title: str) -> None:
        row = store.get(message.chat.id)
        ent = entity_of(row)
        if ent is None:
            await message.answer(NO_GROUP, reply_markup=KEYBOARD)
            return
        try:
            lessons = await fetch(ent, row, start, end)
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
            "Кнопка «🔔 Уведомления» включает расписание по утрам и напоминания перед парами "
            "(по умолчанию всё выключено).\n\n"
            "Если ты преподаватель, отправь /teacher и затем фамилию.",
            reply_markup=KEYBOARD,
        )

    @dp.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(
            "/group — выбрать группу\n/teacher — выбрать преподавателя\n"
            "/notify — уведомления (утреннее расписание, напоминания о парах, подгруппа)\n"
            "/morning 07:45 — время утреннего расписания\n"
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
        await show_day(message, today(), edit=False)

    @dp.message(F.text == BTN_TOMORROW)
    async def btn_tomorrow(message: Message) -> None:
        await show_day(message, today() + timedelta(days=1), edit=False)

    @dp.message(F.text == BTN_WEEK)
    async def btn_week(message: Message) -> None:
        start_d, end_d = week_bounds(today())
        await show_range(message, start_d, end_d, f"неделя {start_d:%d.%m}–{end_d:%d.%m}")

    @dp.message(F.text == BTN_NEXT)
    async def btn_next(message: Message) -> None:
        start_d, end_d = week_bounds(today() + timedelta(days=7))
        await show_range(message, start_d, end_d, f"неделя {start_d:%d.%m}–{end_d:%d.%m}")

    @dp.callback_query(F.data.startswith("day:"))
    async def on_day(call: CallbackQuery) -> None:
        try:
            d = date.fromisoformat(call.data.split(":", 1)[1])
        except ValueError:
            await call.answer()
            return
        # защита от случайного ухода на годы вперёд/назад
        if abs((d - today()).days) > 400:
            await call.answer("Слишком далеко", show_alert=True)
            return
        await call.answer()
        await show_day(call.message, d, edit=True)

    @dp.callback_query(F.data.startswith("week:"))
    async def on_week(call: CallbackQuery) -> None:
        try:
            d = date.fromisoformat(call.data.split(":", 1)[1])
        except ValueError:
            await call.answer()
            return
        await call.answer()
        start_d, end_d = week_bounds(d)
        await show_range(call.message, start_d, end_d, f"неделя {start_d:%d.%m}–{end_d:%d.%m}")

    # ----- уведомления -----
    NEED_GROUP = "Сначала выбери группу: напиши её название, например <code>Г.ГК.24.Б2</code>."

    @dp.message(Command("notify"))
    @dp.message(F.text == BTN_NOTIFY)
    async def notify_menu(message: Message) -> None:
        row = store.get(message.from_user.id)
        if not row or not row["eid"]:
            await message.answer(NEED_GROUP, reply_markup=KEYBOARD)
            return
        await message.answer(settings_text(row), reply_markup=settings_markup(row))

    @dp.message(Command("morning"))
    async def set_morning(message: Message, command: CommandObject) -> None:
        uid = message.from_user.id
        row = store.get(uid)
        if not row or not row["eid"]:
            await message.answer(NEED_GROUP, reply_markup=KEYBOARD)
            return
        m = TIME_ARG_RE.match((command.args or "").strip())
        if not m:
            await message.answer("Напиши время так: <code>/morning 07:45</code>")
            return
        value = f"{int(m.group(1)):02d}:{m.group(2)}"
        store.set_fields(uid, morning_time=value, notify_morning=1, last_morning="")
        await message.answer(
            f"Готово: расписание на день будет приходить в <b>{value}</b> по московскому времени ☀️\n"
            "Выключить можно в «🔔 Уведомления».",
            reply_markup=KEYBOARD,
        )

    @dp.callback_query(F.data.startswith("set:"))
    async def on_setting(call: CallbackQuery) -> None:
        uid = call.from_user.id
        row = store.get(uid)
        if not row or not row["eid"]:
            await call.answer("Сначала выбери группу.", show_alert=True)
            return
        parts = call.data.split(":")  # set:morning | set:pairs | set:off | set:sg:<n>
        action = parts[1] if len(parts) > 1 else ""
        if action == "morning":
            store.set_fields(uid, notify_morning=0 if row["notify_morning"] else 1, last_morning="")
        elif action == "pairs":
            store.set_fields(uid, notify_pairs=0 if row["notify_pairs"] else 1)
        elif action == "off":
            store.set_fields(uid, notify_morning=0, notify_pairs=0)
        elif action == "sg":
            value = parts[2] if len(parts) > 2 else ""
            if value in ("", "1", "2"):
                store.set_fields(uid, subgroup=value)
        row = store.get(uid)
        await call.answer()
        try:
            await call.message.edit_text(settings_text(row), reply_markup=settings_markup(row))
        except TelegramBadRequest:
            pass  # ничего не изменилось (нажали уже выбранный вариант)

    # ----- выбор группы / преподавателя -----
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
        ruz = RuzClient(session, base)
        dp = build_dispatcher(ruz, store)
        notifier = asyncio.create_task(Notifier(bot, ruz, store).run_forever())
        try:
            await dp.start_polling(bot)
        finally:
            notifier.cancel()


if __name__ == "__main__":
    asyncio.run(main())
