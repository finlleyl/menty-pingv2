from datetime import date

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
    assert next_free_col(rows, 0) == 3
    assert next_free_col(ROWS_REAL, 0) == 5
