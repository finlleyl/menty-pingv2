import re

# «Спринт 3», «3 спринт», «Спринт3» — в листе встречаются все варианты
_SPRINT_RE = re.compile(r"спринт\s*([1-9])|([1-9])\s*спринт", re.IGNORECASE)

STAGE_LABELS = {
    "sprint1": "1-й спринт обучения",
    "sprint2": "2-й спринт обучения",
    "sprint3": "3-й спринт обучения",
    "sprint4": "4-й спринт: практика и подготовка к выходу на рынок",
    "interviews": "проходит собеседования",
    "market": "активный поиск работы",
    "paused": "на паузе",
    "offer": "получил оффер",
    "unknown": "стадия неизвестна",
}


def parse_stage(status: str | None) -> str:
    """Строка статуса из таблицы → стадия ученика. Регистр и порядок слов не важны."""
    s = (status or "").strip().lower()
    if not s:
        return "unknown"
    m = _SPRINT_RE.search(s)
    if m:
        n = m.group(1) or m.group(2)
        return f"sprint{n}" if n in ("1", "2", "3", "4") else "unknown"
    if "оффер" in s or "договор" in s:
        return "offer"
    if "собес" in s:
        return "interviews"
    if "рынок" in s or "поиск работы" in s:
        return "market"
    if "приостанов" in s or "занят" in s or "пауз" in s:
        return "paused"
    return "unknown"
