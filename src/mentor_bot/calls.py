"""Календарь созвонов с учениками: собеседования по итогам спринтов и мок-собесы.

Договорённость бот находит в переписке сам. Сначала дешёвый предфильтр по словам (время,
согласие, отмена, разговор о созвоне), потом модель называет день, время и повод. День она
не высчитывает, а выбирает из календаря в запросе: «в четверг» иначе легко уезжает на неделю.
Итог сверяет код: прошедшее время и дату дальше трёх месяцев не записываем, номер спринта без
явного упоминания берём из таблицы. У ученика один предстоящий созвон: новая договорённость
переносит записанный, а не плодит дубли, поэтому повторный разбор той же переписки ничего
не меняет."""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from mentor_bot.cards import NOOP, WEEKDAYS, card_id, day_label, open_chat_row
from mentor_bot.health import alert_once
from mentor_bot.llm import LLMUnavailable
from mentor_bot.pings import parse_iso_utc
from mentor_bot.stages import SPRINTS, parse_stage

log = logging.getLogger(__name__)

WINDOW = timedelta(days=3)       # сколько переписки до новых сообщений видит модель
WINDOW_LIMIT = 20                # и не больше стольких сообщений
GRACE = timedelta(hours=2)       # начавшийся созвон ещё можно перенести или отменить
HORIZON = timedelta(days=90)     # дальше — скорее ошибка разбора, чем договорённость
# На «Собесах», «Рынке» и после оффера «собес в четверг» — это компании, а не ментор:
# по регламенту ментор созванивается только по спринтам и на мок
COMPANY_STAGES = ("interviews", "market", "offer")
MORNING_KEY = "calls_morning_day"   # день последней утренней сводки: она не задвоится

BY_CHAT = "chat"       # договорённость нашёл бот в переписке
BY_HAND = "manual"     # записал ментор командой /call

# Предфильтр: модель зовём, только если в переписке есть время и разговор о созвоне.
# Ложное срабатывание стоит одного запроса (модель ответит none), пропуск — созвона в календаре
_DAY = (r"сегодня|завтра|понедельник|вторник|\bсред[ауые]\b|четверг|пятниц|суббот|воскресень"
        r"|\b(?:пн|вт|ср|чт|пт|сб|вс)\b|\b\d{1,2}[./]\d{1,2}\b"
        r"|\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)")
_HOUR = r"\b\d{1,2}:\d{2}\b|\b[вк]\s+\d{1,2}\b|\bутр[оа]м?\b|вечер|\bдн[её]м\b|после\s+обеда"
_WHEN_RE = re.compile(f"{_DAY}|{_HOUR}", re.IGNORECASE)
_CALL_RE = re.compile(r"собес|\bмок|звон|встреч|\bзум|zoom|телемост|\bmeet|дискорд|discord",
                      re.IGNORECASE)
_AGREE_RE = re.compile(
    r"договорил|забил|подходит|устраивает|удобно|\bок\b|\bok\b|окей|\bго\b|давай|\bда\b|норм"
    r"|супер|отлично|согласен|принято|\bбуду\b|(?:^|\s)\+(?:\s|$)|👍",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"перен[её]с|перенос|отмен|не смогу|не получится|не успева|сдвин",
                        re.IGNORECASE)


def worth_checking(new: list[str], earlier: list[str], booked: bool) -> bool:
    """Звать ли модель. Новые сообщения должны что-то менять: называть время, соглашаться или
    отменять, а рядом должен быть разговор о созвоне со временем. Записанный созвон меняют
    только новым временем или отменой: «ок» и «👍» после договорённости модель не зовут."""
    fresh = "\n".join(new)
    if booked:
        return bool(_WHEN_RE.search(fresh) or _CANCEL_RE.search(fresh))
    if not (_WHEN_RE.search(fresh) or _AGREE_RE.search(fresh) or _CANCEL_RE.search(fresh)):
        return False
    talk = "\n".join(earlier + new)
    return bool(_CALL_RE.search(talk) and _WHEN_RE.search(talk))


_HHMM_RE = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*$")


def parse_hhmm(s: str | None) -> time | None:
    """«19:00», «19.00», «19» → time; иначе None."""
    m = _HHMM_RE.match(s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def resolve_slot(day: str | None, hhmm: str | None, tz: ZoneInfo,
                 now_utc: datetime) -> datetime | None:
    """День и время от модели → момент в UTC. None — не разобрать, уже прошло или подозрительно
    далеко: такой созвон лучше не записать, чем записать не туда."""
    try:
        d = date.fromisoformat((day or "").strip())
    except ValueError:
        return None
    t = parse_hhmm(hhmm)
    if t is None:
        return None
    at = datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)
    if at < now_utc - GRACE or at > now_utc + HORIZON:
        return None
    return at


def call_topic(kind: str | None, sprint: int | None, stage: str) -> tuple[str, int | None]:
    """Повод созвона: модель видит переписку, стадия из таблицы — запасной источник. Номер
    спринта — названный в переписке, иначе текущий: собес по спринту проходит, пока ученик
    на нём, а дальше статус двигает вердикт ментора."""
    if kind not in ("sprint", "mock", "other"):
        kind = ("sprint" if stage in SPRINTS
                else "mock" if stage in ("legend", "mock") else "other")
    if kind != "sprint":
        return kind, None
    if sprint in (1, 2, 3, 4):
        return kind, sprint
    return kind, int(stage[-1]) if stage in SPRINTS else None


def topic_label(kind: str, sprint: int | None) -> str:
    if kind == "sprint":
        return f"собес по спринту {sprint}" if sprint else "собес по спринту"
    if kind == "mock":
        return "мок-собес"
    return "созвон"


def when_label(at: datetime, tz: ZoneInfo) -> str:
    local = at.astimezone(tz)
    return f"{day_label(local)} в {local:%H:%M}"


def _what(c, tz: ZoneInfo) -> str:
    return (f"{when_label(parse_iso_utc(c['starts_at']), tz)}, "
            f"{topic_label(c['kind'], c['sprint'])}")


def _quote(quote: str | None) -> str:
    q = " ".join((quote or "").split())[:100]
    return f"\n«{q}»" if q else ""


def call_kb(cid: int, username: str, active: bool = True) -> InlineKeyboardMarkup:
    """У записанного созвона — «Удалить», у удалённого — «Вернуть»: ошибку разбора и промах
    пальцем исправить одним тапом."""
    if active:
        row = [InlineKeyboardButton(text="🗑 Удалить из календаря", callback_data=f"call:del:{cid}")]
    else:
        row = [InlineKeyboardButton(text="🗑 Удалён", callback_data=NOOP),
               InlineKeyboardButton(text="↩️ Вернуть", callback_data=f"call:back:{cid}")]
    return InlineKeyboardMarkup(inline_keyboard=[row, open_chat_row(username)])


async def book(repo, sender, username: str, at: datetime, kind: str, sprint: int | None, *,
               source: str, tz: ZoneInfo, now_utc: datetime, quote: str | None = None) -> bool:
    """Записать созвон или перенести предстоящий. False — ровно так уже записано."""
    at_iso = at.astimezone(timezone.utc).isoformat()
    now_iso = now_utc.isoformat()
    what = topic_label(kind, sprint)
    old = await repo.upcoming_call(username, (now_utc - GRACE).isoformat())
    if old is None:
        cid = await repo.add_call(username, at_iso, kind, sprint, source, quote, now_iso)
        text = f"📅 Записал в календарь: @{username} — {what}\n{when_label(at, tz)}"
    else:
        same_time = old["starts_at"] == at_iso
        # повторный разбор той же переписки повод не перетирает: модель могла назвать его
        # иначе, чем в прошлый раз. Поправить повод при том же времени может только ментор
        if same_time and (source == BY_CHAT or (old["kind"], old["sprint"]) == (kind, sprint)):
            return False
        cid = old["id"]
        await repo.update_call(cid, at_iso, kind, sprint, source, quote, now_iso)
        await sender.close_card(old.get("card_msg_id"), "➡️ Изменён — карточка ниже", username)
        if same_time:
            text = f"📅 Поправил: @{username} — {what}\n{when_label(at, tz)}"
        else:
            was = when_label(parse_iso_utc(old["starts_at"]), tz)
            text = f"📅 Перенёс: @{username} — {what}\n{was} → {when_label(at, tz)}"
    msg = await sender.notify_mentor(text + _quote(quote), reply_markup=call_kb(cid, username))
    await repo.set_card("calls", cid, card_id(msg))
    return True


async def unbook(repo, sender, call: dict, *, tz: ZoneInfo, now_utc: datetime,
                 quote: str | None = None):
    """Убрать созвон из календаря. На карточке — «Вернуть»: отмену модель могла понять не так."""
    username = call["username"]
    await repo.set_call_state(call["id"], "cancelled", now_utc.isoformat())
    await sender.close_card(call.get("card_msg_id"), "🗑 Отменён — карточка ниже", username)
    msg = await sender.notify_mentor(
        f"🗑 Убрал из календаря: @{username} — {_what(call, tz)}{_quote(quote)}",
        reply_markup=call_kb(call["id"], username, active=False),
    )
    await repo.set_card("calls", call["id"], card_id(msg))


async def calls_cycle(service, repo, llm, sender, settings, now_utc: datetime | None = None):
    """Ищет в свежей переписке договорённости о созвонах. Чат разбираем, когда он утих:
    «в чт в 19?» — «го» — «ой, давай в 20» модель увидит целиком, а не по кускам."""
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.tz_name)
    settle = timedelta(minutes=settings.debounce_minutes)
    errors = 0
    for row in await repo.calls_unseen():
        username = row["username"]
        if now_utc - parse_iso_utc(row["last_ts"]) < settle:
            continue   # переписка ещё идёт — отметку не двигаем, разберём, когда утихнет
        m = service.by_username.get(username)
        if m is not None and parse_stage(m.status) not in COMPANY_STAGES:
            try:
                await _check_chat(username, m, row["seen_id"], repo, llm, sender, tz, now_utc)
            except LLMUnavailable as e:
                # провайдер лежит: отметку не двигаем — следующий тик повторит. Ментора
                # предупреждает разбор сообщений, он упирается в то же самое
                log.warning("LLM unavailable, call check for %s postponed: %s", username, e)
                break
            except Exception:
                # повтор той же переписки, скорее всего, упадёт так же — не жжём запросы
                log.exception("call check failed for %s", username)
                errors += 1
        await repo.set_calls_seen(username, row["max_id"])
    await alert_once(repo, sender, "calls_errors",
                     "⚠️ Не смог разобрать договорённости о созвонах в части чатов — проверь "
                     "календарь (/week) и логи" if errors else "")


async def _check_chat(username, m, seen_id, repo, llm, sender, tz, now_utc):
    rows = await repo.call_window(username, (now_utc - WINDOW).isoformat(), WINDOW_LIMIT)
    new = [r for r in rows if r["id"] > seen_id]
    if not new:
        return
    earlier = [r for r in rows if r["id"] <= seen_id]
    booked = await repo.upcoming_call(username, (now_utc - GRACE).isoformat())
    if not worth_checking([r["text"] for r in new], [r["text"] for r in earlier],
                          booked is not None):
        return
    upd = await llm.extract_call(earlier, new, now_local=now_utc.astimezone(tz),
                                 status=m.status, booked=_what(booked, tz) if booked else None)
    if upd.action == "cancelled":
        if booked:
            await unbook(repo, sender, booked, tz=tz, now_utc=now_utc, quote=upd.quote)
        return
    if upd.action != "scheduled":
        return
    at = resolve_slot(upd.date, upd.time, tz, now_utc)
    if at is None:
        log.warning("call for %s not booked: unusable slot %r %r", username, upd.date, upd.time)
        return
    kind, sprint = call_topic(upd.kind, upd.sprint, parse_stage(m.status))
    await book(repo, sender, username, at, kind, sprint, source=BY_CHAT, tz=tz,
               now_utc=now_utc, quote=upd.quote)


def _day_start(d: date, tz: ZoneInfo) -> str:
    return datetime.combine(d, time(0), tzinfo=tz).astimezone(timezone.utc).isoformat()


async def _calls_by_day(repo, first: date, days: int, tz: ZoneInfo) -> dict[date, list[dict]]:
    out: dict[date, list[dict]] = {}
    for c in await repo.calls_between(_day_start(first, tz),
                                      _day_start(first + timedelta(days=days), tz)):
        out.setdefault(parse_iso_utc(c["starts_at"]).astimezone(tz).date(), []).append(c)
    return out


def _line(c, tz: ZoneInfo) -> str:
    at = parse_iso_utc(c["starts_at"]).astimezone(tz)
    return f"• {at:%H:%M} @{c['username']} — {topic_label(c['kind'], c['sprint'])}"


async def morning_text(repo, settings, now_utc: datetime) -> str | None:
    """Созвоны на сегодня и завтра. None — их нет: пустая сводка каждое утро — шум."""
    tz = ZoneInfo(settings.tz_name)
    today = now_utc.astimezone(tz).date()
    days = await _calls_by_day(repo, today, 2, tz)
    if not days:
        return None
    lines = ["📅 Созвоны"]
    for d, name in ((today, "Сегодня"), (today + timedelta(days=1), "Завтра")):
        lines.append(f"{name}, {day_label(d)}:")
        lines += [_line(c, tz) for c in days.get(d, [])] or ["• нет"]
    return "\n".join(lines)


WEEK_MAX_DAYS = 31
TEXT_LIMIT = 4000   # у Telegram 4096 символов на сообщение — длиннее не уйдёт вовсе


async def week_text(args: str, repo, settings, now_utc: datetime) -> str:
    """/week [дней] — созвоны с сегодняшнего дня, по умолчанию на 7 дней."""
    n = int(args.strip()) if args.strip().isdigit() else 7
    n = max(1, min(n, WEEK_MAX_DAYS))
    tz = ZoneInfo(settings.tz_name)
    today = now_utc.astimezone(tz).date()
    days = await _calls_by_day(repo, today, n, tz)
    if not days:
        return (f"📅 На {n} дн. созвонов нет.\n"
                f"Записать вручную: /call @ник 15.10 19:00 спринт 2")
    out = f"📅 Созвоны на {n} дн.:"
    for d in sorted(days):
        note = (" — сегодня" if d == today
                else " — завтра" if d == today + timedelta(days=1) else "")
        block = "\n\n" + "\n".join([f"{day_label(d).capitalize()}{note}"]
                                   + [_line(c, tz) for c in days[d]])
        if len(out) + len(block) > TEXT_LIMIT:
            out += "\n\n… дальше не влезло в сообщение — попроси /week на меньше дней"
            break
        out += block
    return out


async def calls_morning(repo, sender, settings, now_utc: datetime | None = None):
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_utc.astimezone(ZoneInfo(settings.tz_name)).date().isoformat()
    if await repo.get_setting(MORNING_KEY) == today:
        return   # уже была: догонялка после рестарта и обычный запуск сводку не задвоят
    text = await morning_text(repo, settings, now_utc)
    if text:
        await sender.notify_mentor(text)
    await repo.set_setting(MORNING_KEY, today)


async def morning_due(repo, settings, now_utc: datetime) -> bool:
    """Бот лежал в час утренней сводки — после старта её стоит догнать: созвон может быть
    уже сегодня вечером. Как и остальные догонялки — только если сводка уже работала."""
    last = await repo.get_setting(MORNING_KEY)
    local = now_utc.astimezone(ZoneInfo(settings.tz_name))
    return bool(last) and local.hour >= settings.calls_hour and last < local.date().isoformat()


# /call — запись вручную: договорились голосом, в другом мессенджере, или бот не понял
CALL_USAGE = ("Формат: /call @ник 15.10 19:00 [спринт 2|мок] — записать созвон "
              "(день: 15.10, сегодня, завтра, чт); /call @ник отмена — убрать")
_CANCEL_WORDS = ("отмена", "отменить", "удалить", "off", "-")
_REL_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_WEEKDAY_STEMS = ("пон", "вто", "сре", "чет", "пят", "суб", "вос")
_DATE_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?$")
_TOPIC_SPRINT_RE = re.compile(r"спринт\w*\s*([1-4])|([1-4])\s*-?\w*\s*спринт", re.IGNORECASE)


@dataclass
class CallRequest:
    username: str
    cancel: bool = False
    at: datetime | None = None
    kind: str | None = None
    sprint: int | None = None


def _weekday(token: str) -> int | None:
    if token in WEEKDAYS:
        return WEEKDAYS.index(token)
    for i, stem in enumerate(_WEEKDAY_STEMS):
        if token.startswith(stem):
            return i
    return None


def _resolve_day(token: str, t: time, now_local: datetime) -> datetime | None:
    """«15.10», «сегодня», «чт» → момент по часам ментора. День недели — ближайший впереди."""
    tz, today = now_local.tzinfo, now_local.date()
    token = token.lower().rstrip(",")
    if token in _REL_DAYS:
        return datetime.combine(today + timedelta(days=_REL_DAYS[token]), t, tzinfo=tz)
    wd = _weekday(token)
    if wd is not None:
        at = datetime.combine(today + timedelta(days=(wd - today.weekday()) % 7), t, tzinfo=tz)
        return at if at >= now_local else at + timedelta(days=7)
    m = _DATE_RE.match(token)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            d = date(int(year) + (2000 if len(year) == 2 else 0), month, day)
        else:
            # год не указан — ближайшая такая дата: 05.01 в декабре — это январь следующего года
            d = date(today.year, month, day)
            if d < today:
                d = date(today.year + 1, month, day)
    except ValueError:
        return None
    return datetime.combine(d, t, tzinfo=tz)


def parse_call_args(args: str, now_utc: datetime, tz: ZoneInfo) -> CallRequest:
    """Аргументы /call → запрос. ValueError с подсказкой — не разобрать."""
    tokens = args.split()
    if len(tokens) < 2 or not re.fullmatch(r"@?\w+", tokens[0]):
        raise ValueError(CALL_USAGE)
    username = tokens[0].lstrip("@").lower()
    if len(tokens) == 2 and tokens[1].lower() in _CANCEL_WORDS:
        return CallRequest(username, cancel=True)
    rest = [t for t in tokens[1:] if t.lower() != "в"]   # «чт в 19:00»
    t = parse_hhmm(rest[1]) if len(rest) >= 2 else None
    now_local = now_utc.astimezone(tz)
    at = _resolve_day(rest[0], t, now_local) if t is not None else None
    if at is None:
        raise ValueError(CALL_USAGE)
    if at < now_local:
        raise ValueError("Это время уже прошло")
    topic = " ".join(rest[2:]).lower()
    kind, sprint = None, None
    m = _TOPIC_SPRINT_RE.search(topic)
    if m:
        kind, sprint = "sprint", int(m.group(1) or m.group(2))
    elif "спринт" in topic:
        kind = "sprint"
    elif "мок" in topic:
        kind = "mock"
    elif topic:
        kind = "other"
    return CallRequest(username, at=at.astimezone(timezone.utc), kind=kind, sprint=sprint)


async def handle_call(args: str, service, repo, sender, now_utc: datetime) -> str | None:
    """/call: записать, перенести или убрать созвон. None — ментору ушла карточка, отвечать
    отдельно не нужно; строка — ответ на команду."""
    tz = ZoneInfo(service.settings.tz_name)
    try:
        req = parse_call_args(args, now_utc, tz)
    except ValueError as e:
        return str(e)
    m = service.by_username.get(req.username)
    if m is None:
        return f"@{req.username} нет в таблице"
    if req.cancel:
        booked = await repo.upcoming_call(req.username, (now_utc - GRACE).isoformat())
        if booked is None:
            return f"У @{req.username} нет записанного созвона"
        await unbook(repo, sender, booked, tz=tz, now_utc=now_utc)
        return None
    kind, sprint = call_topic(req.kind, req.sprint, parse_stage(m.status))
    if not await book(repo, sender, req.username, req.at, kind, sprint, source=BY_HAND, tz=tz,
                      now_utc=now_utc):
        return "Уже записано ровно так"
    return None
