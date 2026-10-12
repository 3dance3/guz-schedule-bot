"""Телеграм-бот с расписанием ГУЗ (ruz.guz.ru). Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import hashlib
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
    BufferedInputFile,
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

DEFAULT_REMIND = 10  # за сколько минут до пары напоминать по умолчанию
REMIND_CHOICES = [5, 10, 15, 30]  # варианты в настройках
MORNING_WINDOW_MIN = 90  # сколько минут после выбранного времени ещё можно прислать утреннее расписание
DEFAULT_MORNING = "07:30"
CHANGE_WINDOW_DAYS = 14  # на сколько дней вперёд следим за изменениями
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
TIME_ARG_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SUBGROUP_RE = re.compile(r"\((\d+)\)\s*$")

BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "➡️ Завтра"
BTN_NOW = "⏱ Что сейчас"
BTN_WEEK = "🗓 Неделя"
BTN_ICS = "📆 В календарь"
BTN_NOTIFY = "🔔 Уведомления"
BTN_CHANGE = "🔄 Сменить группу"

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_NOW), KeyboardButton(text=BTN_WEEK)],
        [KeyboardButton(text=BTN_ICS), KeyboardButton(text=BTN_NOTIFY)],
        [KeyboardButton(text=BTN_CHANGE)],
    ],
    resize_keyboard=True,
)

esc = html.escape


# ---------- хранилище настроек пользователей ----------
class Store:
    FIELDS = {
        "notify_morning", "notify_pairs", "notify_changes", "morning_time",
        "remind_min", "subgroup", "last_morning", "snapshot_done",
    }

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
            ("notify_changes", "INTEGER NOT NULL DEFAULT 0"),
            ("morning_time", f"TEXT NOT NULL DEFAULT '{DEFAULT_MORNING}'"),
            ("remind_min", f"INTEGER NOT NULL DEFAULT {DEFAULT_REMIND}"),
            ("subgroup", "TEXT NOT NULL DEFAULT ''"),
            ("last_morning", "TEXT NOT NULL DEFAULT ''"),
            ("snapshot_done", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS sent (user_id INTEGER, key TEXT, PRIMARY KEY (user_id, key))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots (user_id INTEGER, day TEXT, hash TEXT, "
            "PRIMARY KEY (user_id, day))"
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
            "AND (notify_morning=1 OR notify_pairs=1 OR notify_changes=1)"
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

    def get_snapshot(self, user_id: int) -> dict[str, str]:
        return {
            r["day"]: r["hash"]
            for r in self.conn.execute("SELECT day, hash FROM snapshots WHERE user_id=?", (user_id,))
        }

    def replace_snapshot(self, user_id: int, mapping: dict[str, str]) -> None:
        with self.conn:  # одна транзакция
            self.conn.execute("DELETE FROM snapshots WHERE user_id=?", (user_id,))
            self.conn.executemany(
                "INSERT INTO snapshots(user_id, day, hash) VALUES(?, ?, ?)",
                [(user_id, d, h) for d, h in mapping.items()],
            )
            self.conn.execute("UPDATE users SET snapshot_done=1 WHERE user_id=?", (user_id,))

    def drop_snapshot(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM snapshots WHERE user_id=?", (user_id,))
            self.conn.execute("UPDATE users SET snapshot_done=0 WHERE user_id=?", (user_id,))


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
    second = [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"day:{d.isoformat()}")]
    if d != base:
        second.append(InlineKeyboardButton(text="📅 Сегодня", callback_data=f"day:{base.isoformat()}"))
    second.append(InlineKeyboardButton(text="🗓 Неделя", callback_data=f"week:{d.isoformat()}"))
    return InlineKeyboardMarkup(inline_keyboard=[row, second])


def week_nav_markup(monday: date) -> InlineKeyboardMarkup:
    prev_m, next_m = monday - timedelta(days=7), monday + timedelta(days=7)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"◄ {prev_m:%d.%m}", callback_data=f"week:{prev_m.isoformat()}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"week:{monday.isoformat()}"),
        InlineKeyboardButton(text=f"{next_m:%d.%m} ►", callback_data=f"week:{next_m.isoformat()}"),
    ]])


# ---------- «что сейчас / дальше» ----------
def parse_hm(s: str) -> tuple[int, int] | None:
    m = TIME_RE.search(s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_delta(minutes: int) -> str:
    h, m = divmod(max(minutes, 0), 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def now_next_text(ent: Entity, lessons: list[Lesson], now: datetime) -> str:
    """Текущая идущая пара и/или ближайшая следующая на сегодня."""
    show_teacher = ent.kind == GROUP
    header = f"<b>{esc(ent.label)}</b> — {now:%H:%M}"
    timed = []
    for l in lessons:
        b, e = parse_hm(l.begin), parse_hm(l.end)
        if not b:
            continue
        start = datetime.combine(now.date(), dtime(*b), tzinfo=now.tzinfo)
        end = datetime.combine(now.date(), dtime(*e), tzinfo=now.tzinfo) if e else start
        timed.append((start, end, l))
    timed.sort(key=lambda x: x[0])

    current = [(s, e, l) for s, e, l in timed if s <= now <= e]
    nxt = next(((s, e, l) for s, e, l in timed if s > now), None)

    def mins_until(ts: datetime) -> int:  # округление вверх до минуты
        return int((ts - now).total_seconds() + 59) // 60

    parts = [header]
    if current:
        for s, e, l in current:
            parts.append(f"\n🟢 <b>Сейчас идёт</b> (до конца {fmt_delta(mins_until(e))}):\n" +
                         "\n".join(format_lesson(l, show_teacher, ent.label, with_time=False)))
    if nxt:
        s, e, l = nxt
        parts.append(f"\n⏭ <b>Следующая через {fmt_delta(mins_until(s))}</b> (в {s:%H:%M}):\n" +
                     "\n".join(format_lesson(l, show_teacher, ent.label, with_time=False)))
    if not current and not nxt:
        parts.append("\nНа сегодня пар больше нет 🎉" if timed else "\nСегодня пар нет 🎉")
    return "\n".join(parts)


# ---------- экспорт в календарь (.ics) ----------
def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    # RFC 5545: длинные строки переносим; продолжения начинаются с пробела.
    # Режем по символам, чтобы не разорвать многобайтовый UTF-8.
    out, cur, cur_bytes, first = [], "", 0, True
    for ch in line:
        b = len(ch.encode("utf-8"))
        limit = 73 if first else 72  # у продолжений ещё ведущий пробел
        if cur_bytes + b > limit:
            out.append(cur)
            cur, cur_bytes, first = "", 0, False
        cur += ch
        cur_bytes += b
    out.append(cur)
    return "\r\n ".join(out)


def build_ics(ent: Entity, lessons: list[Lesson], tz: ZoneInfo) -> bytes:
    from datetime import timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    show_teacher = ent.kind == GROUP
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//guz-schedule-bot//RU", "CALSCALE:GREGORIAN",
           f"X-WR-CALNAME:{_ics_escape('Расписание ' + ent.label)}"]
    for i, l in enumerate(lessons):
        b, e = parse_hm(l.begin), parse_hm(l.end)
        if not b:
            continue
        start = datetime.combine(l.day, dtime(*b), tzinfo=tz).astimezone(timezone.utc)
        end_hm = e or (b[0] + 1, b[1])
        end = datetime.combine(l.day, dtime(min(end_hm[0], 23), end_hm[1]), tzinfo=tz).astimezone(timezone.utc)
        summary = l.title or "Занятие"
        if l.kind:
            summary += f" ({l.kind})"
        loc = ", ".join(p for p in (l.room, l.building) if p)
        desc_parts = []
        if show_teacher and l.teacher:
            desc_parts.append(l.teacher)
        if l.group:
            desc_parts.append(l.group)
        uid = hashlib.sha1(f"{ent.eid}{l.day}{l.begin}{summary}{i}".encode()).hexdigest()
        out += [
            "BEGIN:VEVENT",
            f"UID:{uid}@guz-schedule-bot",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
            f"DTEND:{end:%Y%m%dT%H%M%SZ}",
            _ics_fold(f"SUMMARY:{_ics_escape(summary)}"),
        ]
        if loc:
            out.append(_ics_fold(f"LOCATION:{_ics_escape(loc)}"))
        if desc_parts:
            out.append(_ics_fold(f"DESCRIPTION:{_ics_escape(' · '.join(desc_parts))}"))
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return ("\r\n".join(out) + "\r\n").encode("utf-8")


# ---------- снимок расписания для уведомлений об изменениях ----------
def lesson_sig(l: Lesson) -> str:
    return "|".join([l.begin, l.end, l.title, l.kind, l.room, l.building, l.teacher, l.group])


def window_hashes(lessons: list[Lesson], start: date, days: int) -> dict[str, str]:
    """Хэш расписания на каждый день окна (пустой день тоже имеет хэш)."""
    by_day: dict[date, list[str]] = {}
    for l in lessons:
        by_day.setdefault(l.day, []).append(lesson_sig(l))
    result = {}
    for i in range(days):
        d = start + timedelta(days=i)
        sigs = sorted(by_day.get(d, []))
        result[d.isoformat()] = hashlib.sha1("\n".join(sigs).encode()).hexdigest()
    return result


# ---------- настройки уведомлений (текст и кнопки) ----------
def _onoff(value) -> str:
    return "ВКЛ ✅" if value else "ВЫКЛ ❌"


def settings_text(row: sqlite3.Row) -> str:
    remind = row["remind_min"]
    lines = [
        "🔔 <b>Уведомления</b>",
        f"Расписание: <b>{esc(row['label'] or '')}</b>",
        "",
        f"☀️ Расписание на день утром: <b>{_onoff(row['notify_morning'])}</b>",
        f"    Время: <b>{esc(row['morning_time'])}</b> (изменить: <code>/morning 07:45</code>)",
        f"⏰ Напоминание за <b>{remind}</b> мин до пары: <b>{_onoff(row['notify_pairs'])}</b>",
        "    Покажу название пары и аудиторию.",
        f"🔄 Сообщать об изменениях в расписании: <b>{_onoff(row['notify_changes'])}</b>",
        "    Напишу, если перенесут пару, сменят аудиторию или отменят.",
    ]
    if row["kind"] == GROUP:
        sg = row["subgroup"]
        lines.append(f"👥 Подгруппа: <b>{esc(sg) if sg else 'все'}</b> (влияет и на расписание, и на уведомления)")
    lines += ["", "Время по московскому. Каждый пункт включается и выключается отдельно кнопками ниже."]
    return "\n".join(lines)


def settings_markup(row: sqlite3.Row) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"☀️ Утром: {_onoff(row['notify_morning'])}", callback_data="set:morning")],
        [InlineKeyboardButton(
            text=f"⏰ За {row['remind_min']} мин до пары: {_onoff(row['notify_pairs'])}", callback_data="set:pairs"
        )],
        [InlineKeyboardButton(text=f"⏱ Напоминать за: {row['remind_min']} мин ⟳", callback_data="set:remind")],
        [InlineKeyboardButton(text=f"🔄 Об изменениях: {_onoff(row['notify_changes'])}", callback_data="set:changes")],
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
    """Раз в минуту смотрит, кому пора отправить утро, напоминание о паре или сообщение об изменениях."""

    def __init__(self, bot: Bot, ruz: RuzClient, store: Store):
        self.bot, self.ruz, self.store = bot, ruz, store
        self.cache: dict[tuple, tuple[float, list[Lesson]]] = {}

    async def _range(self, ent: Entity, start: date, end: date) -> list[Lesson]:
        """Расписание на диапазон с кэшем 15 минут — один запрос на группу для всех её студентов."""
        now_m = time.monotonic()
        for k in [k for k, (t, _) in self.cache.items() if now_m - t > 3600]:
            del self.cache[k]
        key = (ent.stype, ent.eid, start, end)
        hit = self.cache.get(key)
        if hit and now_m - hit[0] < 900:
            return hit[1]
        lessons = await self.ruz.schedule(ent, start, end)
        self.cache[key] = (now_m, lessons)
        return lessons

    async def _send(self, user_id: int, text: str) -> None:
        await self.bot.send_message(user_id, text)
        await asyncio.sleep(0.05)  # не упираемся в лимиты Telegram при большой рассылке

    def _view(self, row, ent: Entity, lessons: list[Lesson]) -> list[Lesson]:
        return for_subgroup(lessons, row["subgroup"]) if ent.kind == GROUP else lessons

    async def handle_user(self, row: sqlite3.Row, now: datetime, check_changes: bool) -> None:
        uid = row["user_id"]
        today = now.date()
        ent = Entity(row["kind"], row["stype"], row["eid"], row["label"])
        show_teacher = ent.kind == GROUP
        remind = row["remind_min"] or DEFAULT_REMIND
        today_view: list[Lesson] | None = None

        async def day_lessons() -> list[Lesson]:
            nonlocal today_view
            if today_view is None:
                today_view = self._view(row, ent, await self._range(ent, today, today))
            return today_view

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
            lessons = await day_lessons()
            by_start: dict[tuple[int, int], list[Lesson]] = {}
            for l in lessons:
                hm = parse_hm(l.begin)
                if hm:
                    by_start.setdefault(hm, []).append(l)
            for (h, m), group in sorted(by_start.items()):
                start = datetime.combine(today, dtime(h, m), tzinfo=TZ)
                if start - timedelta(minutes=remind) <= now < start:
                    key = f"{today.isoformat()}|{h:02d}:{m:02d}"
                    if self.store.was_sent(uid, key):
                        continue
                    left = int((start - now).total_seconds() + 59) // 60
                    body = "\n\n".join(
                        "\n".join(format_lesson(l, show_teacher, ent.label, with_time=False)) for l in group
                    )
                    text = f"⏰ <b>Через {left} мин пара</b> (в {h:02d}:{m:02d})\n\n{body}"
                    await self._send(uid, text)
                    self.store.mark_sent(uid, key)

        # 3) Изменения в расписании (проверяем реже, по флагу check_changes)
        if row["notify_changes"] and check_changes:
            window = self._view(row, ent, await self._range(ent, today, today + timedelta(days=CHANGE_WINDOW_DAYS - 1)))
            new_hashes = window_hashes(window, today, CHANGE_WINDOW_DAYS)
            old_hashes = self.store.get_snapshot(uid)
            if not row["snapshot_done"] or not old_hashes:
                # Первый снимок (или после перезапуска с чистой базой) — просто запоминаем, без спама.
                self.store.replace_snapshot(uid, new_hashes)
            else:
                changed = sorted(d for d, h in new_hashes.items() if old_hashes.get(d) != h)
                if changed:
                    self.store.replace_snapshot(uid, new_hashes)
                    by_day: dict[date, list[Lesson]] = {}
                    for l in window:
                        by_day.setdefault(l.day, []).append(l)
                    blocks = []
                    for diso in changed:
                        d = date.fromisoformat(diso)
                        day_ls = sorted(by_day.get(d, []), key=lambda l: (l.begin, l.title))
                        head = f"📅 <b>{WEEKDAYS[d.weekday()]}, {d:%d.%m}</b>"
                        if day_ls:
                            body = "\n".join(
                                "\n".join(format_lesson(l, show_teacher, ent.label)) for l in day_ls
                            )
                            blocks.append(f"{head}\n\n{body}")
                        else:
                            blocks.append(f"{head}\n\nПар больше нет")
                    header = f"🔄 <b>Расписание изменилось</b> — {esc(ent.label)}"
                    for chunk in split_message(header, blocks):
                        await self._send(uid, chunk)

    async def run_once(self, now: datetime) -> None:
        check_changes = now.minute % 15 == 0  # изменения проверяем раз в 15 минут
        for row in self.store.notify_users():
            try:
                await self.handle_user(row, now, check_changes)
            except TelegramForbiddenError:
                # Пользователь заблокировал бота: больше ему не пишем.
                self.store.set_fields(row["user_id"], notify_morning=0, notify_pairs=0, notify_changes=0)
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

    async def show_week(message: Message, monday: date, edit: bool) -> None:
        row = store.get(message.chat.id)
        ent = entity_of(row)
        if ent is None:
            await message.answer(NO_GROUP, reply_markup=KEYBOARD)
            return
        sunday = monday + timedelta(days=6)
        try:
            lessons = await fetch(ent, row, monday, sunday)
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
        header = f"<b>{esc(ent.label)}</b> — неделя {monday:%d.%m}–{sunday:%d.%m}"
        blocks = format_blocks(lessons, show_teacher=ent.kind == GROUP, own_label=ent.label) or ["Пар нет 🎉"]
        chunks = split_message(header, blocks)
        nav = week_nav_markup(monday)
        if edit and len(chunks) == 1:
            try:
                await message.edit_text(chunks[0], reply_markup=nav)
            except TelegramBadRequest:
                pass
            return
        for i, chunk in enumerate(chunks):
            await message.answer(chunk, reply_markup=(nav if i == len(chunks) - 1 else None))

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        store.set_mode(message.from_user.id, GROUP)
        await message.answer(
            "Привет! Я показываю расписание ГУЗ.\n\n"
            "Напиши название своей группы, например <code>Г.ГК.24.Б2</code>, "
            "и я её запомню. Дальше пользуйся кнопками внизу: день можно листать ◄ ►, "
            "«⏱ Что сейчас» покажет ближайшую пару, «📆 В календарь» выгрузит расписание в телефон.\n\n"
            "Кнопка «🔔 Уведомления» включает расписание по утрам, напоминания перед парами и "
            "сообщения об изменениях в расписании (по умолчанию всё выключено).\n\n"
            "Если ты преподаватель, отправь /teacher и затем фамилию.",
            reply_markup=KEYBOARD,
        )

    @dp.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(
            "<b>Что умею:</b>\n"
            "📅 Сегодня / Завтра — день с кнопками ◄ ► для перелистывания\n"
            "⏱ Что сейчас — текущая или ближайшая пара с отсчётом\n"
            "🗓 Неделя — вся неделя, тоже листается ◄ ►\n"
            "📆 В календарь — выгрузка в Google/Apple Календарь (.ics)\n"
            "🔔 Уведомления — утром расписание, за N минут до пары, и сообщения об изменениях\n\n"
            "<b>Команды:</b>\n"
            "/group — выбрать группу\n/teacher — расписание преподавателя\n"
            "/notify — настройки уведомлений\n/morning 07:45 — время утреннего расписания\n"
            "/now — что сейчас\n/calendar — выгрузка в календарь",
            reply_markup=KEYBOARD,
        )

    @dp.message(Command("now"))
    async def cmd_now(message: Message) -> None:
        await btn_now(message)

    @dp.message(Command("calendar"))
    async def cmd_calendar(message: Message) -> None:
        await btn_ics(message)

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
        await show_week(message, week_bounds(today())[0], edit=False)

    @dp.message(F.text == BTN_NOW)
    async def btn_now(message: Message) -> None:
        row = store.get(message.chat.id)
        ent = entity_of(row)
        if ent is None:
            await message.answer(NO_GROUP, reply_markup=KEYBOARD)
            return
        now = datetime.now(TZ)
        try:
            lessons = await fetch(ent, row, now.date(), now.date())
        except RuzError as e:
            await message.answer(f"⚠️ {esc(str(e))}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await message.answer("⚠️ Сайт расписания сейчас не отвечает. Попробуй чуть позже.")
            return
        except Exception:
            log.exception("now failed")
            await message.answer("⚠️ Что-то пошло не так. Попробуй ещё раз.")
            return
        await message.answer(now_next_text(ent, lessons, now), reply_markup=KEYBOARD)

    @dp.message(F.text == BTN_ICS)
    async def btn_ics(message: Message) -> None:
        row = store.get(message.chat.id)
        ent = entity_of(row)
        if ent is None:
            await message.answer(NO_GROUP, reply_markup=KEYBOARD)
            return
        start = today()
        end = start + timedelta(days=CHANGE_WINDOW_DAYS - 1)
        try:
            lessons = await fetch(ent, row, start, end)
        except RuzError as e:
            await message.answer(f"⚠️ {esc(str(e))}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await message.answer("⚠️ Сайт расписания сейчас не отвечает. Попробуй чуть позже.")
            return
        except Exception:
            log.exception("ics failed")
            await message.answer("⚠️ Что-то пошло не так. Попробуй ещё раз.")
            return
        if not lessons:
            await message.answer("На ближайшие 2 недели пар нет — нечего выгружать.", reply_markup=KEYBOARD)
            return
        data = build_ics(ent, lessons, TZ)
        fname = re.sub(r"[^A-Za-z0-9._-]+", "_", f"raspisanie_{ent.label}") + ".ics"
        await message.answer_document(
            BufferedInputFile(data, filename=fname),
            caption=(
                "📆 Расписание на 2 недели.\n\n"
                "Открой файл на телефоне → события добавятся в календарь (Google/Apple). "
                "Там же можно включить напоминания. Файл разовый: после изменений пришли /calendar ещё раз."
            ),
            reply_markup=KEYBOARD,
        )

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
        if abs((d - today()).days) > 400:
            await call.answer("Слишком далеко", show_alert=True)
            return
        await call.answer()
        await show_week(call.message, week_bounds(d)[0], edit=True)

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
        parts = call.data.split(":")  # set:morning|pairs|remind|changes|off | set:sg:<n>
        action = parts[1] if len(parts) > 1 else ""
        if action == "morning":
            store.set_fields(uid, notify_morning=0 if row["notify_morning"] else 1, last_morning="")
        elif action == "pairs":
            store.set_fields(uid, notify_pairs=0 if row["notify_pairs"] else 1)
        elif action == "remind":
            cur = row["remind_min"] if row["remind_min"] in REMIND_CHOICES else DEFAULT_REMIND
            nxt = REMIND_CHOICES[(REMIND_CHOICES.index(cur) + 1) % len(REMIND_CHOICES)]
            store.set_fields(uid, remind_min=nxt)
        elif action == "changes":
            if row["notify_changes"]:
                store.set_fields(uid, notify_changes=0)
            else:
                # включаем: сбрасываем снимок, чтобы базовое состояние снялось тихо, без «изменилось»
                store.drop_snapshot(uid)
                store.set_fields(uid, notify_changes=1)
        elif action == "off":
            store.set_fields(uid, notify_morning=0, notify_pairs=0, notify_changes=0)
        elif action == "sg":
            value = parts[2] if len(parts) > 2 else ""
            if value in ("", "1", "2") and value != row["subgroup"]:
                store.set_fields(uid, subgroup=value)
                store.drop_snapshot(uid)  # подгруппа сменилась — базу изменений пересоберём заново
        row = store.get(uid)
        await call.answer()
        try:
            await call.message.edit_text(settings_text(row), reply_markup=settings_markup(row))
        except TelegramBadRequest:
            pass  # ничего не изменилось (нажали уже выбранный вариант)

    # ----- выбор группы / преподавателя -----
    async def select(message: Message, user_id: int, ent: Entity) -> None:
        store.set_selection(user_id, ent)
        store.drop_snapshot(user_id)  # новая группа — базу для «изменений» собираем заново
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
