import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

log = logging.getLogger(__name__)

# таблица с русской локалью отдаёт «12.08.2025», руками пишут и «12/08/25», и ISO
_DATE_FORMATS = ("%d/%m/%Y", "%d.%m.%Y", "%d/%m/%y", "%d.%m.%y", "%Y-%m-%d")


def parse_date(s: str):
    try:
        s = s.strip()
    except AttributeError:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def format_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


_USERNAME = re.compile(
    # @ник, но не e-mail: перед @ нет локальной части адреса, после ника нет «.домен»
    r"(?<![\w.+-])@(\w+)\b(?!\.\w)"
    r"|(?<![\w.])(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(\w+)\b",
    re.IGNORECASE,
)
# служебные пути t.me — это не ники: инвайты, стикеры, посты каналов
_TME_SERVICE = {"joinchat", "addstickers", "addlist", "share", "proxy", "socks", "c", "s", "iv"}


def extract_username(cell: str):
    for m in _USERNAME.finditer(cell or ""):
        name = m.group(1) or m.group(2)
        if m.group(2) and name.lower() in _TME_SERVICE:
            continue
        return name.lower()
    return None


@dataclass
class HeaderMap:
    header_row: int
    mentee_col: int
    date_col: int
    status_col: int
    notes_col: int | None = None      # «Заметочки» — только чтение
    dossier_col: int | None = None    # «Досье» — только запись
    names: dict[str, str] = field(default_factory=dict)   # роль → заголовок, для отчёта


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
    mentee_col: int = 0    # 1-based; 0 — строку перед записью не сверяем
    header_row: int = 0    # 1-based; 0 — колонки перед записью не сверяем


@dataclass
class SheetReport:
    """Итог чтения одного листа: сломанный лист больше не пропадает молча."""
    title: str
    ok: bool
    problem: str | None = None
    columns: dict[str, str] = field(default_factory=dict)          # роль → заголовок
    mentees: int = 0
    # (строка 1-based, причина): без ника строка пропущена, с неразобранной датой
    # менти загружен без даты
    skipped: list[tuple[int, str]] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # роль → заголовки-претенденты


class RowNotFound(Exception):
    """Менти больше нет в листе: строку удалили или ник поменяли."""


class StatusConflict(Exception):
    """Статус в таблице уже не тот, от которого считалось предложение."""

    def __init__(self, current: str):
        super().__init__(current)
        self.current = current


class SheetSchemaChanged(Exception):
    """Колонку для записи не найти однозначно: её удалили, переименовали или появилась
    вторая похожая. Писать наугад нельзя — данные легли бы в чужую колонку."""

    def __init__(self, title: str, problem: str):
        super().__init__(f"{title}: {problem}")
        self.title = title
        self.problem = problem


ROLES = ("mentee", "date", "status", "notes", "dossier")
REQUIRED = ("mentee", "date", "status")
ROLE_LABELS = {"mentee": "менти", "date": "дата контакта", "status": "статус",
               "notes": "заметки", "dossier": "досье"}
# порядок — приоритет: при «Дата» и «Дата пинга» в одном листе дата — «Дата пинга»
_PREFERRED = {
    "mentee": ("менти", "ученик", "студент"),
    "date": ("дата пинга", "дата контакта", "последний контакт", "дата"),
    "status": ("статус", "статус менти", "этап"),
    "notes": ("заметочки", "заметки"),
    "dossier": ("досье",),
}
_GENERIC = {
    "mentee": re.compile(r"\b(менти|ученик|студент)"),
    "date": re.compile(r"^дата"),
    "status": re.compile(r"\b(статус|этап)"),
    "notes": re.compile(r"\bзамет(к|очк)"),
    "dossier": re.compile(r"\bдосье"),
}
# слова, с которыми колонка точно про другое: «Дата старта» — не дата контакта,
# «Статус оплаты» — не стадия ученика, «Статус менти» — не колонка с менти
_EXCLUDE = {
    "mentee": re.compile(r"статус|этап|дата|замет|досье"),
    "date": re.compile(r"старт|рожд|оплат|договор|начал|конец|оконч"),
    "status": re.compile(r"оплат|договор|^дата"),
}
_EXACT = 50   # очки точного имени — выше, общего признака — ниже

NO_HEADERS = ("не узнал заголовки: нужны «Дата пинга» и «Статус» в первых 6 строках "
              "(или HEADER_DATE и HEADER_STATUS в .env)")
NO_MENTEE = ("не нашёл колонку менти: нет заголовка «Менти» и @username под шапкой "
             "(или HEADER_MENTEE в .env)")
NO_USERNAME = "нет @username"
BAD_DATE = "дата не разобрана"


def _norm(cell) -> str:
    return " ".join((cell or "").split()).lower()


def _cell(row, col) -> str:
    if col is None or col >= len(row):
        return ""
    return (row[col] or "").strip()


def _col_letter(col: int) -> str:
    """1-based номер колонки → буквы, как в Sheets."""
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _label(row, col: int) -> str:
    return _cell(row, col) or f"колонка {_col_letter(col + 1)}"


def _score(role: str, head: str) -> int:
    """Точное имя из списка — 100 минус место в списке, общий признак — 10, иначе 0."""
    ex = _EXCLUDE.get(role)
    if not head or (ex and ex.search(head)):
        return 0
    names = _PREFERRED[role]
    if head in names:
        return 100 - names.index(head)
    return 10 if _GENERIC[role].search(head) else 0


def _resolve(row, overrides) -> tuple[dict[str, int], dict[str, list[int]]]:
    """Роль → колонка (0-based) по строке заголовков и роль → колонки, поделившие
    первое место: обязательную роль при ничьей не угадываем."""
    heads = [_norm(c) for c in row]
    cols: dict[str, int] = {}
    ties: dict[str, list[int]] = {}
    for role in ROLES:
        want = _norm(overrides.get(role))
        hits = [i for i, h in enumerate(heads) if want and h == want]
        if len(hits) == 1:
            cols[role] = hits[0]
        elif hits:
            ties[role] = hits
    # сначала точные имена всех ролей, потом общие признаки: иначе общий признак одной
    # роли мог бы забрать колонку, названную ровно под другую
    for exact in (True, False):
        for role in ROLES:
            if role in cols or role in ties:
                continue
            taken = set(cols.values())
            scored = [(s, i) for i, h in enumerate(heads)
                      if i not in taken and (s := _score(role, h)) and (s > _EXACT) == exact]
            if not scored:
                continue
            best = max(s for s, _ in scored)
            top = [i for s, i in scored if s == best]
            if len(top) > 1 and role in REQUIRED:
                ties[role] = top
            else:
                cols[role] = top[0]   # «Заметки»/«Досье» при ничьей — левая, как раньше
    return cols, ties


def _guess_mentee_col(rows, header_row: int, exclude: set[int]):
    """Колонка менти по данным: где чаще всего встречается @username."""
    counts: dict[int, int] = {}
    for row in rows[header_row + 1 : header_row + 16]:
        for ci, cell in enumerate(row):
            if ci not in exclude and extract_username(cell):
                counts[ci] = counts.get(ci, 0) + 1
    return max(counts, key=counts.get) if counts else None


@dataclass
class HeaderScan:
    hm: HeaderMap | None
    problem: str | None = None
    ambiguous: dict[str, list[str]] = field(default_factory=dict)


def scan_headers(rows, overrides: dict[str, str] | None = None) -> HeaderScan:
    """overrides — роль → точный заголовок из .env (HEADER_*), регистр не важен."""
    overrides = overrides or {}
    problem = NO_HEADERS
    for ri, row in enumerate(rows[:6]):
        cols, ties = _resolve(row, overrides)
        if not all(r in cols or r in ties for r in ("date", "status")):
            continue  # не похоже на строку заголовков
        amb = {r: [_label(row, i) for i in ties[r]] for r in REQUIRED if r in ties}
        if amb:
            text = "; ".join(f"неясно, где {ROLE_LABELS[r]}: " + " или ".join(f"«{n}»" for n in names)
                             for r, names in amb.items())
            return HeaderScan(None, text, amb)
        if "mentee" not in cols:
            # заголовка «Менти» нет (например «Column 1») — ищем по содержимому
            guess = _guess_mentee_col(rows, ri, set(cols.values()))
            if guess is None:
                problem = NO_MENTEE
                continue
            cols["mentee"] = guess
        names = {r: _label(row, i) for r, i in cols.items()}
        return HeaderScan(HeaderMap(ri, cols["mentee"], cols["date"], cols["status"],
                                    cols.get("notes"), cols.get("dossier"), names))
    return HeaderScan(None, problem)


def map_headers(rows, overrides: dict[str, str] | None = None):
    return scan_headers(rows, overrides).hm


def next_free_col(rows) -> int:
    """0-based индекс первой колонки правее любой непустой ячейки листа. Смотрим все
    строки, а не только шапку: колонка без заголовка, но с данными — тоже занята."""
    return max((i for row in rows for i, c in enumerate(row) if (c or "").strip()), default=-1) + 1


def _collect(title: str, rows, scan: HeaderScan) -> tuple[list[SheetMentee], SheetReport]:
    hm = scan.hm
    if hm is None:
        return [], SheetReport(title, False, scan.problem, ambiguous=scan.ambiguous)
    out, skipped = [], []
    for ri in range(hm.header_row + 1, len(rows)):
        row = rows[ri]
        display = _cell(row, hm.mentee_col)
        username = extract_username(display)
        if not username:
            if display:   # пустая строка — не проблема, имя без ника — да: бот его не видит
                skipped.append((ri + 1, NO_USERNAME))
            continue
        raw_date = _cell(row, hm.date_col)
        last_date = parse_date(raw_date)
        if raw_date and last_date is None:
            # менти не выкидываем: без даты его всё равно ведёт переписка
            skipped.append((ri + 1, f"{BAD_DATE}: «{raw_date}»"))
        out.append(SheetMentee(
            username=username, display=display, status=_cell(row, hm.status_col) or None,
            last_date=last_date, sheet_title=title,
            row=ri + 1, date_col=hm.date_col + 1, status_col=hm.status_col + 1,
            notes=_cell(row, hm.notes_col), dossier=_cell(row, hm.dossier_col),
            dossier_col=(hm.dossier_col + 1) if hm.dossier_col is not None else 0,
            mentee_col=hm.mentee_col + 1, header_row=hm.header_row + 1,
        ))
    report = SheetReport(title, True, columns=dict(hm.names), mentees=len(out), skipped=skipped)
    return out, report


def read_sheet(title: str, rows, overrides: dict[str, str] | None = None):
    """(менти, отчёт) по значениям листа."""
    return _collect(title, rows, scan_headers(rows, overrides))


def parse_sheet(title: str, rows, overrides: dict[str, str] | None = None) -> list[SheetMentee]:
    return read_sheet(title, rows, overrides)[0]


def _in_rows(nums: list[int]) -> str:
    if len(nums) == 1:
        return f"в строке {nums[0]}"
    head = ", ".join(map(str, nums[:5]))
    return f"в строках {head}" + (f" и ещё {len(nums) - 5}" if len(nums) > 5 else "")


def report_problems(reports: list[SheetReport]) -> list[str]:
    """Короткие строки ментору: какие листы и строки бот не смог прочитать."""
    lines = []
    for r in reports:
        if not r.ok:
            line = f"⚠️ Лист «{r.title}»: {r.problem}"
            for role, names in r.ambiguous.items():
                if len({_norm(n) for n in names}) < len(names):
                    # одинаковые заголовки HEADER_* не различит — только переименовать
                    line += f". Переименуй одну из колонок «{names[0]}»"
                else:
                    line += f". Переименуй лишнюю или укажи нужную в .env: HEADER_{role.upper()}=<заголовок>"
            lines.append(line)
            continue
        no_nick = [n for n, why in r.skipped if why == NO_USERNAME]
        bad_date = [(n, why) for n, why in r.skipped if why.startswith(BAD_DATE)]
        if no_nick:
            lines.append(f"⚠️ Лист «{r.title}»: нет @username {_in_rows(no_nick)} — "
                         f"такие строки бот не видит")
        if bad_date:
            example = bad_date[0][1].split(": ", 1)[1]
            lines.append(f"⚠️ Лист «{r.title}»: {BAD_DATE} {_in_rows([n for n, _ in bad_date])} "
                         f"(например {example}) — для бота даты нет; формат 12.08.2025")
    return lines


def _write(ws, row: int, col: int, value: str, *, raw: bool) -> None:
    """raw — текст как есть: статус или досье с «=» в начале при USER_ENTERED стали бы
    формулой или #ERROR!. Дату пишем USER_ENTERED, чтобы Sheets понял её как дату."""
    ws.update(values=[[value]], range_name=f"{_col_letter(col)}{row}",
              value_input_option="RAW" if raw else "USER_ENTERED")


def _ensure_grid(ws, row: int, col: int) -> None:
    """Запись за краем сетки листа API отвергает: append_row расширял лист сам, теперь мы."""
    if row > ws.row_count:
        ws.add_rows(row - ws.row_count)
    if col > ws.col_count:
        ws.add_cols(col - ws.col_count)


# Карта колонок перед записью кэшируется на лист: цикл досье пишет десятки строк подряд,
# и чтение шапки на каждую запись съедало бы квоту Sheets API (60 чтений в минуту).
# Цена — колонку, вставленную меньше минуты назад, бот увидит только после истечения кэша.
HEADER_TTL = 60.0


class SheetsClient:
    def __init__(self, sa_path: str, spreadsheet_id: str, titles: list[str],
                 overrides: dict[str, str] | None = None, header_ttl: float = HEADER_TTL):
        self._sa_path = sa_path
        self._spreadsheet_id = spreadsheet_id
        self._titles = titles
        self._book = None
        self._overrides = {r: v for r, v in (overrides or {}).items() if (v or "").strip()}
        self._header_ttl = header_ttl
        self._headers: dict[str, tuple[float, HeaderMap]] = {}   # лист → (когда, карта)
        self._clock = time.monotonic   # тесты подменяют, чтобы «прожить» минуту
        self.last_report: list[SheetReport] = []

    def _open(self):
        if self._book is None:
            import gspread
            gc = gspread.service_account(filename=self._sa_path)
            self._book = gc.open_by_key(self._spreadsheet_id)
        return self._book

    async def load_mentees(self) -> list[SheetMentee]:
        """Каждый лист читается сам по себе: переименованный лист или сбитая шапка не
        обнуляют остальные. Что не прочиталось — в last_report (см. report_problems)."""
        def work():
            from gspread.exceptions import WorksheetNotFound
            book = self._open()
            result, reports, errors = [], [], []
            for title in self._titles:
                try:
                    rows = book.worksheet(title).get_all_values()
                except WorksheetNotFound as e:
                    errors.append(e)
                    reports.append(SheetReport(title, False, "лист не найден — проверь ACTIVE_SHEETS"))
                    self._headers.pop(title, None)
                    continue
                except Exception as e:
                    log.exception("sheet %s read failed", title)
                    errors.append(e)
                    reports.append(SheetReport(title, False, f"не прочитан: {e}"[:200]))
                    continue
                scan = scan_headers(rows, self._overrides)
                mentees, report = _collect(title, rows, scan)
                if scan.hm is not None:
                    self._headers[title] = (self._clock(), scan.hm)   # шапка свежая — сразу в кэш
                else:
                    self._headers.pop(title, None)
                    log.warning("sheet %s skipped: %s", title, report.problem)
                result.extend(mentees)
                reports.append(report)
            self.last_report = reports
            if errors and len(errors) == len(self._titles):
                # не открылся ни один лист — это сбой доступа, а не пустая таблица:
                # пусть вызывающий оставит прежний кэш, а не решит, что менти больше нет
                raise errors[-1]
            return result
        return await asyncio.to_thread(work)

    def _schema(self, ws, m: SheetMentee) -> HeaderMap:
        hit = self._headers.get(m.sheet_title)
        if hit and self._clock() - hit[0] < self._header_ttl:
            return hit[1]
        scan = scan_headers([ws.row_values(m.header_row)], self._overrides)
        if scan.hm is not None:
            scan.hm.header_row = m.header_row - 1
        elif not scan.ambiguous:
            # колонку менти угадывали по @username или над шапкой вставили строку —
            # по одной строке этого не понять, читаем лист целиком
            scan = scan_headers(ws.get_all_values(), self._overrides)
        if scan.hm is None:
            raise SheetSchemaChanged(m.sheet_title, scan.problem)
        self._headers[m.sheet_title] = (self._clock(), scan.hm)
        return scan.hm

    def _fresh_cols(self, ws, m: SheetMentee) -> HeaderMap | None:
        """Сверка колонок с шапкой перед записью: после вставки колонки старый номер
        указывает на соседнюю, и дата легла бы в чужую колонку. None — сверять не с чем."""
        if not m.header_row:
            return None
        hm = self._schema(ws, m)
        m.header_row = hm.header_row + 1
        m.mentee_col = hm.mentee_col + 1
        m.date_col = hm.date_col + 1
        m.status_col = hm.status_col + 1
        if hm.dossier_col is not None:
            m.dossier_col = hm.dossier_col + 1   # пропажу «Досье» разбирает set_dossier
        return hm

    @staticmethod
    def _locate(ws, m: SheetMentee) -> int:
        """Актуальная строка менти. Кэш строк живёт часами, а лист могли отсортировать
        или вставить строку — писать по старому номеру значит писать в чужую строку.
        Поэтому перед записью сверяем ник и при расхождении ищем его заново."""
        if not m.mentee_col:
            return m.row
        col = ws.col_values(m.mentee_col)
        if m.row <= len(col) and extract_username(col[m.row - 1]) == m.username:
            return m.row
        for i, cell in enumerate(col):
            if extract_username(cell) == m.username:
                m.row = i + 1
                return m.row
        raise RowNotFound(m.username)

    async def set_date(self, m: SheetMentee, d: date):
        def work():
            ws = self._open().worksheet(m.sheet_title)
            self._fresh_cols(ws, m)
            _write(ws, self._locate(ws, m), m.date_col, format_date(d), raw=False)
        await asyncio.to_thread(work)

    async def set_status(self, m: SheetMentee, status: str, expected: str | None = None):
        """expected — статус, от которого считалось предложение. Если в таблице уже
        другой (ментор поменял руками или нажал более свежую кнопку) — StatusConflict."""
        def work():
            ws = self._open().worksheet(m.sheet_title)
            self._fresh_cols(ws, m)
            row = self._locate(ws, m)
            if expected is not None:
                current = (ws.cell(row, m.status_col).value or "").strip()
                if current != expected.strip():
                    raise StatusConflict(current)
            _write(ws, row, m.status_col, status, raw=True)
        await asyncio.to_thread(work)

    async def append_mentee(self, title: str, display: str):
        def work():
            ws = self._open().worksheet(title)
            rows = ws.get_all_values()
            scan = scan_headers(rows, self._overrides)
            if scan.hm is None:
                raise SheetSchemaChanged(title, scan.problem)
            hm = scan.hm
            # append_row без table_range сам ищет «таблицу» в листе и может положить строку
            # со сдвигом; поэтому строку считаем сами — сразу под последним менти
            last = max((ri for ri in range(hm.header_row + 1, len(rows))
                        if _cell(rows[ri], hm.mentee_col)), default=hm.header_row)
            row, col = last + 2, hm.mentee_col + 1   # 0-based last → 1-based следующая
            _ensure_grid(ws, row, col)
            _write(ws, row, col, display, raw=True)
        await asyncio.to_thread(work)

    async def ensure_dossier_column(self, title: str) -> bool:
        """Создаёт колонку «Досье», если её нет. True — колонка была создана."""
        def work():
            ws = self._open().worksheet(title)
            rows = ws.get_all_values()
            scan = scan_headers(rows, self._overrides)
            if scan.hm is None:
                raise SheetSchemaChanged(title, scan.problem)
            if scan.hm.dossier_col is not None:
                return False
            row, col = scan.hm.header_row + 1, next_free_col(rows) + 1  # 1-based
            _ensure_grid(ws, row, col)
            _write(ws, row, col, "Досье", raw=True)
            self._headers.pop(title, None)   # карта колонок листа изменилась
            return True
        return await asyncio.to_thread(work)

    async def set_dossier(self, m: SheetMentee, text: str):
        if not m.dossier_col and not m.header_row:
            return  # колонки «Досье» нет и сверить не с чем — писать некуда
        def work():
            ws = self._open().worksheet(m.sheet_title)
            hm = self._fresh_cols(ws, m)
            if hm is not None and hm.dossier_col is None:
                if m.dossier_col:
                    raise SheetSchemaChanged(m.sheet_title, "пропала колонка «Досье»")
                return  # колонки «Досье» в листе нет — писать некуда
            # колонку могли создать после синхронизации — _fresh_cols её уже подхватил
            _write(ws, self._locate(ws, m), m.dossier_col, text, raw=True)
        await asyncio.to_thread(work)
