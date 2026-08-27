import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime


def parse_date(s: str):
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


def format_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def extract_username(cell: str):
    m = re.search(r"@(\w+)", cell or "")
    return m.group(1).lower() if m else None


@dataclass
class HeaderMap:
    header_row: int
    mentee_col: int
    date_col: int
    status_col: int
    notes_col: int | None = None      # «Заметочки» — только чтение
    dossier_col: int | None = None    # «Досье» — только запись


@dataclass
class SheetMentee:
    username: str
    display: str
    status: str | None
    last_date: date | None
    sheet_title: str
    row: int          # 1-based
    date_col: int     # 1-based
    status_col: int   # 1-based
    notes: str = ""
    dossier: str = ""
    dossier_col: int = 0   # 1-based; 0 — колонки «Досье» в листе нет


def _guess_mentee_col(rows, header_row: int, exclude: set[int]):
    """Колонка менти по данным: где чаще всего встречается @username."""
    counts: dict[int, int] = {}
    for row in rows[header_row + 1 : header_row + 16]:
        for ci, cell in enumerate(row):
            if ci not in exclude and extract_username(cell):
                counts[ci] = counts.get(ci, 0) + 1
    return max(counts, key=counts.get) if counts else None


def map_headers(rows):
    for ri, row in enumerate(rows[:6]):
        mentee_col = date_col = status_col = None
        notes_col = dossier_col = None
        for ci, cell in enumerate(row):
            c = (cell or "").strip().lower()
            if "менти" in c and "статус" not in c and mentee_col is None:
                mentee_col = ci
            elif c.startswith("дата") and date_col is None:
                date_col = ci
            elif "статус" in c and status_col is None:
                status_col = ci
            elif ("заметк" in c or "заметочк" in c) and notes_col is None:
                notes_col = ci
            elif "досье" in c and dossier_col is None:
                dossier_col = ci
        if date_col is not None and status_col is not None:
            if mentee_col is None:
                # заголовка «Менти» нет (например «Column 1») — ищем по содержимому
                mentee_col = _guess_mentee_col(rows, ri, {date_col, status_col})
            if mentee_col is not None:
                return HeaderMap(ri, mentee_col, date_col, status_col, notes_col, dossier_col)
    return None


def next_free_col(rows, header_row: int) -> int:
    """0-based индекс первой свободной колонки справа от заполненных заголовков."""
    header = rows[header_row] if header_row < len(rows) else []
    last = max((i for i, c in enumerate(header) if (c or "").strip()), default=-1)
    return last + 1


def parse_sheet(title: str, rows) -> list[SheetMentee]:
    hm = map_headers(rows)
    if hm is None:
        return []
    out = []
    for ri in range(hm.header_row + 1, len(rows)):
        row = rows[ri]
        cell = row[hm.mentee_col] if hm.mentee_col < len(row) else ""
        username = extract_username(cell)
        if not username:
            continue
        raw_date = row[hm.date_col] if hm.date_col < len(row) else ""
        status = (row[hm.status_col] if hm.status_col < len(row) else "").strip() or None
        def cell_at(col):
            if col is None or col >= len(row):
                return ""
            return (row[col] or "").strip()

        out.append(SheetMentee(
            username=username, display=cell.strip(), status=status,
            last_date=parse_date(raw_date), sheet_title=title,
            row=ri + 1, date_col=hm.date_col + 1, status_col=hm.status_col + 1,
            notes=cell_at(hm.notes_col), dossier=cell_at(hm.dossier_col),
            dossier_col=(hm.dossier_col + 1) if hm.dossier_col is not None else 0,
        ))
    return out


class SheetsClient:
    def __init__(self, sa_path: str, spreadsheet_id: str, titles: list[str]):
        self._sa_path = sa_path
        self._spreadsheet_id = spreadsheet_id
        self._titles = titles
        self._book = None

    def _open(self):
        if self._book is None:
            import gspread
            gc = gspread.service_account(filename=self._sa_path)
            self._book = gc.open_by_key(self._spreadsheet_id)
        return self._book

    async def load_mentees(self) -> list[SheetMentee]:
        def work():
            book = self._open()
            result = []
            for title in self._titles:
                ws = book.worksheet(title)
                result.extend(parse_sheet(title, ws.get_all_values()))
            return result
        return await asyncio.to_thread(work)

    async def set_date(self, m: SheetMentee, d: date):
        await asyncio.to_thread(
            lambda: self._open().worksheet(m.sheet_title).update_cell(m.row, m.date_col, format_date(d))
        )

    async def set_status(self, m: SheetMentee, status: str):
        await asyncio.to_thread(
            lambda: self._open().worksheet(m.sheet_title).update_cell(m.row, m.status_col, status)
        )

    async def append_mentee(self, title: str, display: str):
        def work():
            ws = self._open().worksheet(title)
            hm = map_headers(ws.get_all_values())
            if hm is None:
                raise RuntimeError(f"no header row in sheet {title!r}")
            row = [""] * (max(hm.mentee_col, hm.date_col, hm.status_col) + 1)
            row[hm.mentee_col] = display
            ws.append_row(row, value_input_option="USER_ENTERED")
        await asyncio.to_thread(work)

    async def ensure_dossier_column(self, title: str) -> bool:
        """Создаёт колонку «Досье», если её нет. True — колонка была создана."""
        def work():
            ws = self._open().worksheet(title)
            rows = ws.get_all_values()
            hm = map_headers(rows)
            if hm is None:
                raise RuntimeError(f"no header row in sheet {title!r}")
            if hm.dossier_col is not None:
                return False
            col = next_free_col(rows, hm.header_row) + 1  # 1-based
            ws.update_cell(hm.header_row + 1, col, "Досье")
            return True
        return await asyncio.to_thread(work)

    async def set_dossier(self, m: SheetMentee, text: str):
        if not m.dossier_col:
            return  # колонки «Досье» в листе нет — писать некуда
        await asyncio.to_thread(
            lambda: self._open().worksheet(m.sheet_title).update_cell(m.row, m.dossier_col, text)
        )
