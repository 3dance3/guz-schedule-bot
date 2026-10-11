"""Клиент открытого API расписания ruz.guz.ru (формат как у РУЗ ВШЭ).

Используемые адреса:
  GET /api/search?type=group&term=<текст>
  GET /api/schedule/<type>/<id>?start=YYYY.MM.DD&finish=YYYY.MM.DD&lng=1
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import aiohttp

GROUP = "group"
TEACHER = "teacher"
# У разных РУЗ преподаватели называются по-разному, пробуем по очереди.
TEACHER_TYPES = ("lecturer", "person", "teacher")


class RuzError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class Entity:
    kind: str  # group | teacher
    stype: str  # значение параметра type в API
    eid: str
    label: str
    description: str = ""


@dataclass
class Lesson:
    day: date
    begin: str
    end: str
    title: str
    kind: str
    room: str
    building: str
    teacher: str
    group: str


def _pick(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_lessons(data: list) -> list[Lesson]:
    lessons: list[Lesson] = []
    seen: set[tuple] = set()
    for x in data:
        if not isinstance(x, dict):
            continue
        if str(x.get("deletion_mark") or "0").strip().lower() in ("1", "true"):
            continue  # отменённое/удалённое занятие
        day = _parse_date(_pick(x, "date", "dateOfLesson"))
        if day is None:
            continue
        lesson = Lesson(
            day=day,
            begin=_pick(x, "beginLesson", "start", "begin"),
            end=_pick(x, "endLesson", "end", "finish"),
            title=_pick(x, "discipline", "disciplineName", "name", "subject"),
            kind=_pick(x, "kindOfWork", "type", "lessonType"),
            room=_pick(x, "auditorium", "room"),
            building=_pick(x, "building", "korpus"),
            teacher=_pick(x, "lecturer", "lecturer_title", "teacher"),
            group=_pick(x, "subGroup", "group", "stream"),
        )
        key = (lesson.day, lesson.begin, lesson.end, lesson.title, lesson.room, lesson.teacher, lesson.group)
        if key in seen:
            continue
        seen.add(key)
        lessons.append(lesson)
    lessons.sort(key=lambda l: (l.day, l.begin, l.title))
    return lessons


class RuzClient:
    def __init__(self, session: aiohttp.ClientSession, base: str = "https://ruz.guz.ru"):
        self.session = session
        self.base = base.rstrip("/")

    async def get_json(self, path: str, params: dict | None = None):
        url = f"{self.base}{path}"
        timeout = aiohttp.ClientTimeout(total=20)
        async with self.session.get(url, params=params, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuzError(f"Сайт расписания ответил ошибкой {resp.status}. Попробуй позже.")
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raise RuzError("Сайт расписания вернул неожиданный ответ. Попробуй позже.")
        if isinstance(data, dict) and data.get("error"):
            raise RuzError(str(data.get("message") or "Сайт расписания вернул ошибку."))
        return data

    async def search(self, kind: str, term: str) -> list[Entity]:
        term = term.strip()
        types = (GROUP,) if kind == GROUP else TEACHER_TYPES
        last_error: RuzError | None = None
        for stype in types:
            try:
                data = await self.get_json("/api/search", {"type": stype, "term": term})
            except RuzError as e:
                if kind == GROUP:
                    raise
                last_error = e
                continue
            if isinstance(data, list) and data:
                return [
                    Entity(
                        kind=kind,
                        stype=stype,
                        eid=str(x["id"]),
                        label=_pick(x, "label", "name", "fullName"),
                        description=_pick(x, "description"),
                    )
                    for x in data
                    if isinstance(x, dict) and x.get("id") is not None
                ]
        if last_error is not None:
            raise last_error
        return []

    async def schedule(self, ent: Entity, start: date, end: date) -> list[Lesson]:
        data = await self.get_json(
            f"/api/schedule/{ent.stype}/{ent.eid}",
            {"start": start.strftime("%Y.%m.%d"), "finish": end.strftime("%Y.%m.%d"), "lng": 1},
        )
        if not isinstance(data, list):
            raise RuzError("Сайт расписания вернул расписание в неожиданном формате.")
        return [l for l in parse_lessons(data) if start <= l.day <= end]
