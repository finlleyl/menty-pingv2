from mentor_bot.config import Settings


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("MENTOR_USER_ID", "42")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("SPREADSHEET_ID", "s")
    monkeypatch.setenv("ACTIVE_SHEETS", "A, B")
    s = Settings(_env_file=None)
    assert s.mentor_user_id == 42
    assert s.active_sheet_titles == ["A", "B"]
    assert "оффер" in s.stop_status_list
    assert s.ping_interval_days == 3


def test_new_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("MENTOR_USER_ID", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("SPREADSHEET_ID", "s")
    monkeypatch.setenv("ACTIVE_SHEETS", "A")
    s = Settings(_env_file=None)
    assert s.debounce_minutes == 5
    assert s.dossier_hour == 3
