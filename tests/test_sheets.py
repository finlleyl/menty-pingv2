import re
from datetime import date

import pytest
from gspread.exceptions import WorksheetNotFound

from mentor_bot.sheets import extract_username, map_headers, parse_date, parse_sheet

ROWS = [
    ["", "", "", "", ""],
    ["", "Заметки", "Менти", "Дата пинга", "Статус менти"],
    ["", "", "Степан @testStepan", "12/08/2025", "Работает"],
    ["", "", "Олег @testOleg", "", "Игнорит"],
    ["", "", "Чел без имени", "03/11/2025", "Собесы"],   # нет @username → пропуск
]


def test_parse_date():
    assert parse_date("12/08/2025") == date(2025, 8, 12)
    assert parse_date("23 сент") is None
    assert parse_date("") is None


def test_extract_username():
    assert extract_username("Степан @testStepan") == "teststepan"
    assert extract_username("Чел без имени") is None


def test_map_headers_and_parse():
    hm = map_headers(ROWS)
    assert hm.header_row == 1 and hm.mentee_col == 2 and hm.date_col == 3 and hm.status_col == 4
    mentees = parse_sheet("Лист1", ROWS)
    assert [m.username for m in mentees] == ["teststepan", "testoleg"]
    m = mentees[0]
    assert m.last_date == date(2025, 8, 12) and m.status == "Работает"
    assert m.row == 3 and m.date_col == 4 and m.status_col == 5  # 1-based для gspread
    assert m.header_row == 2


ROWS_NO_MENTEE_HEADER = [
    ["Column 1", "Заметочки", "Дата пинга", "Статус менти", "Оффер"],
    ["Андрей @testAndrew", "", "21/08/2026", "Поиск работы", ""],
    ["Антон @Kholod", "", "21/08/2026", "Спринт 3", ""],
    ["Чел без ника", "", "21/08/2026", "Спринт 1", ""],
]


def test_map_headers_fallback_guesses_mentee_col():
    hm = map_headers(ROWS_NO_MENTEE_HEADER)
    assert hm is not None
    assert hm.header_row == 0 and hm.mentee_col == 0
    assert hm.date_col == 2 and hm.status_col == 3
    mentees = parse_sheet("X", ROWS_NO_MENTEE_HEADER)
    assert [m.username for m in mentees] == ["testandrew", "kholod"]


from mentor_bot.sheets import next_free_col

ROWS_REAL = [
    ["Column 1", "Заметочки", "Дата пинга", "Статус менти", "Оффер"],
    ["Пётр @testPetr", "слабая база, нужен разбор", "25/08/2026", "Поиск работы", ""],
    ["Семён @testSemen", "", "27/08/2026", "Спринт 3", ""],
]

ROWS_WITH_DOSSIER = [
    ["Column 1", "Заметочки", "Дата пинга", "Статус менти", "Оффер", "Досье"],
    ["Пётр @testPetr", "слабая база, нужен разбор", "25/08/2026", "Поиск работы", "", "Копает Go"],
]


def test_map_headers_finds_notes_column():
    hm = map_headers(ROWS_REAL)
    assert hm.notes_col == 1
    assert hm.dossier_col is None


def test_map_headers_finds_dossier_column():
    hm = map_headers(ROWS_WITH_DOSSIER)
    assert hm.notes_col == 1 and hm.dossier_col == 5


def test_parse_sheet_reads_notes_and_dossier():
    m = parse_sheet("X", ROWS_WITH_DOSSIER)[0]
    assert m.notes == "слабая база, нужен разбор"
    assert m.dossier == "Копает Go"
    assert m.dossier_col == 6            # 1-based для gspread


def test_parse_sheet_without_dossier_column():
    m = parse_sheet("X", ROWS_REAL)[0]
    assert m.notes == "слабая база, нужен разбор"
    assert m.dossier == "" and m.dossier_col == 0


def test_next_free_col_ignores_trailing_empties():
    rows = [["Column 1", "Заметочки", "Дата пинга", "", ""]]
    assert next_free_col(rows) == 3
    assert next_free_col(ROWS_REAL) == 5


class FakeCell:
    def __init__(self, value):
        self.value = value


def _a1_to_rc(a1):
    letters, digits = re.fullmatch(r"([A-Z]+)(\d+)", a1).groups()
    col = 0
    for ch in letters:
        col = col * 26 + ord(ch) - ord("A") + 1
    return int(digits), col


class FakeWorksheet:
    """Лист в памяти: rows[i][j] ↔ ячейка (i+1, j+1)."""

    def __init__(self, rows, row_count=1000, col_count=26):
        self.rows = [list(r) for r in rows]
        self.writes = []    # (строка, колонка, значение)
        self.modes = []     # value_input_option каждой записи
        self.reads = {"row_values": 0, "get_all_values": 0}
        self.row_count = row_count
        self.col_count = col_count

    def get_all_values(self):
        self.reads["get_all_values"] += 1
        width = max((len(r) for r in self.rows), default=0)
        return [r + [""] * (width - len(r)) for r in self.rows]   # gspread добивает до прямоугольника

    def row_values(self, row):
        self.reads["row_values"] += 1
        r = list(self.rows[row - 1]) if row - 1 < len(self.rows) else []
        while r and not r[-1]:
            r.pop()            # gspread хвостовые пустые не отдаёт
        return r

    def col_values(self, col):
        return [r[col - 1] if col - 1 < len(r) else "" for r in self.rows]

    def cell(self, row, col):
        r = self.rows[row - 1] if row - 1 < len(self.rows) else []
        return FakeCell(r[col - 1] if col - 1 < len(r) else "")

    def update_cell(self, row, col, value):
        self._put(row, col, value, "USER_ENTERED")   # так пишет gspread.update_cell

    def update(self, values, range_name, value_input_option=None):
        row, col = _a1_to_rc(range_name)
        for dr, line in enumerate(values):
            for dc, value in enumerate(line):
                self._put(row + dr, col + dc, value, value_input_option)

    def add_rows(self, n):
        self.row_count += n

    def add_cols(self, n):
        self.col_count += n

    def _put(self, row, col, value, mode):
        assert row <= self.row_count and col <= self.col_count, "запись за краем сетки"
        self.writes.append((row, col, value))
        self.modes.append(mode)
        while len(self.rows) < row:
            self.rows.append([])
        r = self.rows[row - 1]
        r.extend([""] * (col - len(r)))
        r[col - 1] = value

    def insert_col(self, index, header_row, header):
        """Ментор вставил колонку: index 0-based, header_row 0-based."""
        for i, r in enumerate(self.rows):
            r.insert(index, header if i == header_row else "")


class FakeBook:
    def __init__(self, ws=None, sheets=None):
        self.ws = ws
        self.sheets = sheets or {}

    def worksheet(self, title):
        if self.ws is not None:
            return self.ws
        item = self.sheets.get(title)
        if item is None:
            raise WorksheetNotFound(title)
        if isinstance(item, Exception):
            raise item
        return item


def client_with(rows, **kw):
    from mentor_bot.sheets import SheetsClient
    c = SheetsClient("sa.json", "id", ["Лист1"], **kw)
    ws = FakeWorksheet(rows)
    c._book = FakeBook(ws)
    return c, ws


async def test_write_follows_row_after_sheet_resort():
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    assert stepan.row == 3
    # ментор отсортировал лист: Олег поднялся, Степан уехал на строку ниже
    ws.rows[2], ws.rows[3] = ws.rows[3], ws.rows[2]
    await c.set_date(stepan, date(2026, 9, 1))
    assert ws.writes == [(4, 4, "01/09/2026")]
    assert stepan.row == 4
    assert ws.rows[2][3] == ""   # строка Олега не тронута


async def test_write_to_vanished_mentee_raises():
    from mentor_bot.sheets import RowNotFound
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    ws.rows[2][2] = "кто-то другой"
    with pytest.raises(RowNotFound):
        await c.set_date(stepan, date(2026, 9, 1))
    assert ws.writes == []


async def test_set_status_compare_and_set():
    from mentor_bot.sheets import StatusConflict
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    with pytest.raises(StatusConflict) as e:
        await c.set_status(stepan, "Собесы", expected="Спринт 4")
    assert e.value.current == "Работает" and ws.writes == []
    await c.set_status(stepan, "Собесы", expected="Работает")
    assert ws.writes == [(3, 5, "Собесы")]


# --- п.8: форматы даты ---

def test_parse_date_accepts_common_formats():
    for s in ("12/08/2025", "12.08.2025", "12/08/25", "12.08.25", "2025-08-12", " 12.08.2025 "):
        assert parse_date(s) == date(2025, 8, 12), s
    assert parse_date("1.8.2025") == date(2025, 8, 1)
    assert parse_date("32.08.2025") is None
    assert parse_date(None) is None


def test_format_date_unchanged():
    from mentor_bot.sheets import format_date
    assert format_date(date(2025, 8, 12)) == "12/08/2025"


# --- п.7: ник, e-mail и ссылки t.me ---

def test_extract_username_ignores_email():
    assert extract_username("ivan@mail.ru") is None
    assert extract_username("Иван ivan.petrov@gmail.com") is None
    assert extract_username("ivan@mail.ru, ник @ivan_p") == "ivan_p"
    assert extract_username("пиши @ivan.") == "ivan"


def test_extract_username_from_tme_links():
    assert extract_username("t.me/IvanP") == "ivanp"
    assert extract_username("Иван https://t.me/ivan_petrov") == "ivan_petrov"
    assert extract_username("http://telegram.me/ivan") == "ivan"
    assert extract_username("https://t.me/joinchat/AAAAB") is None   # инвайт, не ник
    assert extract_username("https://t.me/+AbCdEf") is None
    assert extract_username("start.me/ivan") is None


# --- п.1: роли колонок по очкам ---

def test_date_start_left_of_ping_date_does_not_steal_role():
    rows = [["Менти", "Дата старта", "Статус оплаты", "Дата пинга", "Статус менти"],
            ["@ivan", "01/06/2026", "оплачено", "12/08/2026", "Спринт 2"]]
    hm = map_headers(rows)
    assert hm.mentee_col == 0 and hm.date_col == 3 and hm.status_col == 4
    m = parse_sheet("X", rows)[0]
    assert m.last_date == date(2026, 8, 12) and m.status == "Спринт 2"


def test_preferred_names_win_over_generic_and_plain():
    rows = [["Ученик", "Дата", "Этап", "Дата пинга", "Статус"]]
    hm = map_headers(rows + [["@ivan", "", "", "", ""]])
    assert hm.date_col == 3      # «Дата пинга» важнее просто «Даты»
    assert hm.status_col == 4    # «Статус» важнее «Этапа»
    assert hm.mentee_col == 0


def test_only_excluded_date_columns_means_no_headers():
    from mentor_bot.sheets import scan_headers
    rows = [["Менти", "Дата рождения", "Статус"], ["@ivan", "01/01/2000", "Спринт 1"]]
    scan = scan_headers(rows)
    assert scan.hm is None and "заголовки" in scan.problem


def test_generic_tie_is_ambiguous_not_guessed():
    from mentor_bot.sheets import scan_headers
    rows = [["Менти", "Дата звонка", "Дата сообщения", "Статус"], ["@ivan", "", "", ""]]
    scan = scan_headers(rows)
    assert scan.hm is None
    assert scan.ambiguous == {"date": ["Дата звонка", "Дата сообщения"]}
    assert map_headers(rows) is None and parse_sheet("X", rows) == []


def test_override_resolves_tie_case_insensitive():
    rows = [["Менти", "Дата звонка", "Дата сообщения", "Статус"], ["@ivan", "", "01.09.2026", ""]]
    hm = map_headers(rows, {"date": "  дата СООБЩЕНИЯ "})
    assert hm.date_col == 2
    assert parse_sheet("X", rows, {"date": "Дата сообщения"})[0].last_date == date(2026, 9, 1)


def test_override_from_settings(monkeypatch):
    from mentor_bot.config import Settings
    for k, v in {"BOT_TOKEN": "t", "MENTOR_USER_ID": "1", "LLM_API_KEY": "k",
                 "SPREADSHEET_ID": "s", "ACTIVE_SHEETS": "A", "HEADER_DATE": "Дата звонка"}.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.header_overrides == {"date": "Дата звонка"}
    rows = [["Менти", "Дата звонка", "Дата сообщения", "Статус"], ["@ivan", "", "", ""]]
    assert map_headers(rows, s.header_overrides).date_col == 1


def test_mentee_guess_skips_notes_with_usernames():
    rows = [["Column 1", "Заметочки", "Дата пинга", "Статус"],
            ["Иван @ivan", "спросить у @petr", "", "Спринт 1"],
            ["Олег @oleg", "", "", "Спринт 2"]]
    hm = map_headers(rows)
    assert hm.mentee_col == 0 and hm.notes_col == 1


# --- п.2: отчёт по листам ---

def test_read_sheet_reports_skipped_rows():
    from mentor_bot.sheets import read_sheet
    rows = [["Менти", "Дата пинга", "Статус"],
            ["@ivan", "12.08.2026", "Спринт 1"],
            ["Пётр без ника", "12.08.2026", "Спринт 2"],
            ["", "", ""],                                  # пустая строка — не проблема
            ["@oleg", "23 сент", "Спринт 3"],
            ["ivan@mail.ru", "", ""]]
    mentees, rep = read_sheet("Лист1", rows)
    assert [m.username for m in mentees] == ["ivan", "oleg"]   # с кривой датой менти остаётся
    assert mentees[1].last_date is None
    assert rep.ok and rep.mentees == 2
    assert rep.columns == {"mentee": "Менти", "date": "Дата пинга", "status": "Статус"}
    assert rep.skipped == [(3, "нет @username"), (5, "дата не разобрана: «23 сент»"),
                           (6, "нет @username")]


def _multi_client(sheets, titles):
    from mentor_bot.sheets import SheetsClient
    c = SheetsClient("sa.json", "id", titles)
    c._book = FakeBook(sheets=sheets)
    return c


async def test_load_mentees_survives_broken_sheets():
    good = FakeWorksheet([["Менти", "Дата пинга", "Статус"],
                          ["@ivan", "12/08/2026", "Спринт 1"],
                          ["Без ника", "", ""]])
    ambiguous = FakeWorksheet([["Менти", "Дата", "Дата", "Статус"], ["@petr", "", "", ""]])
    c = _multi_client({"Хороший": good, "Двойной": ambiguous, "Падает": RuntimeError("quota")},
                      ["Пропал", "Двойной", "Падает", "Хороший"])
    mentees = await c.load_mentees()
    assert [m.username for m in mentees] == ["ivan"]
    rep = {r.title: r for r in c.last_report}
    assert [r.title for r in c.last_report] == ["Пропал", "Двойной", "Падает", "Хороший"]
    assert not rep["Пропал"].ok and "не найден" in rep["Пропал"].problem
    assert not rep["Двойной"].ok and rep["Двойной"].ambiguous == {"date": ["Дата", "Дата"]}
    assert not rep["Падает"].ok and "quota" in rep["Падает"].problem
    assert rep["Хороший"].ok and rep["Хороший"].mentees == 1
    assert rep["Хороший"].skipped == [(3, "нет @username")]


async def test_load_mentees_raises_when_nothing_readable():
    c = _multi_client({"Падает": RuntimeError("no access")}, ["Пропал", "Падает"])
    with pytest.raises(RuntimeError):
        await c.load_mentees()
    assert len(c.last_report) == 2      # отчёт есть и при падении


async def test_load_mentees_header_problem_is_not_a_crash():
    bad = FakeWorksheet([["что-то", "другое"], ["@ivan", ""]])
    c = _multi_client({"Лист1": bad}, ["Лист1"])
    assert await c.load_mentees() == []
    assert not c.last_report[0].ok and "заголовки" in c.last_report[0].problem


def test_report_problems_lines():
    from mentor_bot.sheets import SheetReport, report_problems
    reports = [
        SheetReport("Ок", True, columns={"mentee": "Менти"}, mentees=3),
        SheetReport("Пропал", False, "лист не найден — проверь ACTIVE_SHEETS"),
        SheetReport("Две даты", False, "неясно, где дата контакта: «Дата звонка» или «Дата сообщения»",
                    ambiguous={"date": ["Дата звонка", "Дата сообщения"]}),
        SheetReport("Близнецы", False, "неясно, где статус: «Статус» или «Статус»",
                    ambiguous={"status": ["Статус", "Статус"]}),
        SheetReport("Строки", True, mentees=5,
                    skipped=[(3, "нет @username"), (4, "нет @username"),
                             (7, "дата не разобрана: «23 сент»")]),
    ]
    lines = report_problems(reports)
    assert len(lines) == 5                       # у листа «Ок» проблем нет
    assert "«Пропал»" in lines[0] and "не найден" in lines[0]
    assert "HEADER_DATE=" in lines[1]
    assert "Переименуй одну" in lines[2] and "HEADER_" not in lines[2]   # одинаковые не различить
    assert "строках 3, 4" in lines[3] and "@username" in lines[3]
    assert "в строке 7" in lines[4] and "«23 сент»" in lines[4]


def test_report_problems_truncates_row_list():
    from mentor_bot.sheets import SheetReport, report_problems
    rep = SheetReport("Л", True, skipped=[(n, "нет @username") for n in range(2, 10)])
    assert "2, 3, 4, 5, 6 и ещё 3" in report_problems([rep])[0]


# --- п.3: перед записью колонки сверяются с шапкой ---

async def test_write_follows_inserted_column():
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    ws.insert_col(0, header_row=1, header="Город")     # всё съехало на колонку вправо
    await c.set_date(stepan, date(2026, 9, 1))
    assert ws.writes == [(3, 5, "01/09/2026")]         # «Дата пинга» теперь в E
    assert stepan.date_col == 5 and stepan.status_col == 6 and stepan.mentee_col == 4


async def test_write_follows_column_inserted_between_roles():
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    ws.insert_col(3, header_row=1, header="Город")     # между «Менти» и «Дата пинга»
    await c.set_status(stepan, "Собесы", expected="Работает")
    assert ws.writes == [(3, 6, "Собесы")]


async def test_write_refused_when_role_becomes_ambiguous():
    from mentor_bot.sheets import SheetSchemaChanged
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    ws.rows[1][3] = "Дата звонка"
    ws.insert_col(4, header_row=1, header="Дата сообщения")
    with pytest.raises(SheetSchemaChanged) as e:
        await c.set_date(stepan, date(2026, 9, 1))
    assert "дата" in e.value.problem and e.value.title == "Лист1"
    assert ws.writes == [] and stepan.date_col == 4     # карта менти не тронута


async def test_write_refused_when_role_disappears():
    from mentor_bot.sheets import SheetSchemaChanged
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    ws.rows[1][4] = "Комментарий"                      # колонку статуса переименовали
    with pytest.raises(SheetSchemaChanged):
        await c.set_status(stepan, "Собесы")
    assert ws.writes == []


async def test_header_cache_limits_reads():
    c, ws = client_with(ROWS)
    stepan, oleg = parse_sheet("Лист1", ROWS)
    now = [1000.0]
    c._clock = lambda: now[0]
    await c.set_date(stepan, date(2026, 9, 1))
    await c.set_date(oleg, date(2026, 9, 1))
    assert ws.reads["row_values"] == 1                  # вторая запись — из кэша
    ws.insert_col(0, header_row=1, header="Город")
    now[0] += 61
    await c.set_date(stepan, date(2026, 9, 3))
    assert ws.reads["row_values"] == 2                  # кэш истёк — шапку перечитали
    assert ws.writes[-1] == (3, 5, "03/09/2026")


async def test_load_seeds_header_cache():
    c, ws = client_with(ROWS)
    [stepan, _] = await c.load_mentees()
    await c.set_date(stepan, date(2026, 9, 1))
    assert ws.reads == {"row_values": 0, "get_all_values": 1}


async def test_guessed_mentee_column_survives_insert():
    rows = [list(r) for r in ROWS_REAL]                 # «Column 1» — менти угадан по @никам
    c, ws = client_with(rows)
    petr = parse_sheet("Лист1", rows)[0]
    ws.insert_col(0, header_row=0, header="№")
    await c.set_date(petr, date(2026, 9, 1))
    assert ws.writes == [(2, 4, "01/09/2026")]
    assert petr.mentee_col == 2 and ws.reads["get_all_values"] == 1


async def test_write_follows_header_row_moved_down():
    c, ws = client_with(ROWS)
    stepan = parse_sheet("Лист1", ROWS)[0]
    ws.rows.insert(0, ["Список менти", "", "", "", ""])   # строка над шапкой
    await c.set_date(stepan, date(2026, 9, 1))
    assert ws.writes == [(4, 4, "01/09/2026")]
    assert stepan.header_row == 3 and stepan.row == 4


async def test_set_dossier_raises_when_column_vanished():
    from mentor_bot.sheets import SheetSchemaChanged
    rows = [list(r) for r in ROWS_WITH_DOSSIER]
    c, ws = client_with(rows)
    petr = parse_sheet("Лист1", rows)[0]
    ws.rows[0][5] = ""
    with pytest.raises(SheetSchemaChanged):
        await c.set_dossier(petr, "текст")
    assert ws.writes == []


async def test_set_dossier_picks_up_column_created_after_load():
    rows = [list(r) for r in ROWS_REAL]
    c, ws = client_with(rows)
    petr = parse_sheet("Лист1", rows)[0]
    assert petr.dossier_col == 0
    assert await c.ensure_dossier_column("Лист1") is True
    await c.set_dossier(petr, "Копает Go")
    assert ws.writes[-1] == (2, 6, "Копает Go") and petr.dossier_col == 6


async def test_set_dossier_without_column_writes_nothing():
    rows = [list(r) for r in ROWS_REAL]
    c, ws = client_with(rows)
    petr = parse_sheet("Лист1", rows)[0]
    await c.set_dossier(petr, "текст")
    assert ws.writes == []


# --- п.4: «Досье» не ложится на колонку с данными без шапки ---

def test_next_free_col_sees_unlabeled_data():
    rows = [["Column 1", "Дата пинга", "Статус", ""],
            ["@ivan", "", "", "", "", "звонить вечером"]]
    assert next_free_col(rows) == 6


async def test_ensure_dossier_column_right_of_all_data():
    rows = [["Column 1", "Заметочки", "Дата пинга", "Статус менти", ""],
            ["Пётр @testPetr", "", "25/08/2026", "Поиск работы", "без шапки"]]
    c, ws = client_with(rows)
    assert await c.ensure_dossier_column("Лист1") is True
    assert ws.writes == [(1, 6, "Досье")] and ws.modes == ["RAW"]
    assert await c.ensure_dossier_column("Лист1") is False


async def test_ensure_dossier_column_extends_grid():
    rows = [["Менти", "Дата пинга", "Статус"], ["@ivan", "", ""]]
    c, ws = client_with(rows)
    ws.col_count = 3
    assert await c.ensure_dossier_column("Лист1") is True
    assert ws.writes == [(1, 4, "Досье")] and ws.col_count == 4


# --- п.5: новый менти — сразу под последним, а не куда решит append_row ---

async def test_append_mentee_writes_below_last_mentee():
    rows = [["", "", "", ""],
            ["Заметки", "Менти", "Дата пинга", "Статус"],
            ["", "@ivan", "12/08/2026", "Спринт 1"],
            ["", "", "", ""],
            ["", "@oleg", "", "Спринт 2"],
            ["", "", "", ""],
            ["", "", "", "FALSE"]]                     # флажки ниже таблицы — не менти
    c, ws = client_with(rows)
    await c.append_mentee("Лист1", "=petr")
    assert ws.writes == [(6, 2, "=petr")] and ws.modes == ["RAW"]


async def test_append_mentee_into_empty_table_and_full_grid():
    rows = [["Менти", "Дата пинга", "Статус"]]
    c, ws = client_with(rows)
    ws.row_count = 1
    await c.append_mentee("Лист1", "@petr")
    assert ws.writes == [(2, 1, "@petr")] and ws.row_count == 2


async def test_append_mentee_refuses_unrecognized_sheet():
    from mentor_bot.sheets import SheetSchemaChanged
    c, ws = client_with([["что-то", "другое"]])
    with pytest.raises(SheetSchemaChanged):
        await c.append_mentee("Лист1", "@petr")
    assert ws.writes == []


# --- п.6: статус и досье — RAW, дата — USER_ENTERED ---

async def test_value_input_options():
    rows = [list(r) for r in ROWS_WITH_DOSSIER]
    c, ws = client_with(rows)
    petr = parse_sheet("Лист1", rows)[0]
    await c.set_date(petr, date(2026, 9, 1))
    await c.set_status(petr, "=Собесы")
    await c.set_dossier(petr, "=IMPORTXML(...) и прочее")
    assert ws.writes == [(2, 3, "01/09/2026"), (2, 4, "=Собесы"), (2, 6, "=IMPORTXML(...) и прочее")]
    assert ws.modes == ["USER_ENTERED", "RAW", "RAW"]
