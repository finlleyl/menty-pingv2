"""Живые карточки: после действия карточка в личке ментора сама показывает итог, а кнопки
действий с неё исчезают. По ленте сразу видно, что разобрано, а что висит, и старые кнопки
не отвечают «Уже обработано»."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# кнопка-итог: нажатие ничего не делает, только напоминает, что карточка закрыта
NOOP = "noop"
# бот упал посреди отправки: ушло сообщение или нет, знает только чат
UNCERTAIN_LABEL = "⚠️ Бот упал при отправке — проверь чат"


def card_id(msg):
    """message_id отправленной карточки; None, если отправка ничего не вернула."""
    return getattr(msg, "message_id", None)


def chat_url(username: str) -> str:
    return f"https://t.me/{username}"


def open_chat_row(username: str) -> list[InlineKeyboardButton]:
    """«Открыть чат» — до переписки с учеником один тап, без поиска по контактам."""
    return [InlineKeyboardButton(text="💬 Открыть чат", url=chat_url(username))]


def done_kb(label: str, username: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=NOOP)]]
    if username:
        rows.append(open_chat_row(username))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hhmm(tz_name: str, now: datetime | None = None) -> str:
    """Время для итога на карточке — по часам ментора, а не UTC."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def day_label(d) -> str:
    """«чт 08.10» — день недели рядом с датой: договариваются «в четверг», а не «8-го»."""
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def escalation_kb(username: str) -> InlineKeyboardMarkup:
    """Ученик игнорит пинги: пинговать снова, отложить на две недели или написать самому."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Пинговать снова", callback_data=f"esc:reset:{username}"),
         InlineKeyboardButton(text="⏸ Через 14 дн", callback_data=f"esc:pause:{username}:14")],
        open_chat_row(username),
    ])
