from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from mentor_bot.pings import effective_last_contact, in_send_window, is_stopped, should_ping

MSK = ZoneInfo("Europe/Moscow")
STOP = ["умер", "оффер", "приостановил", "договор", "ушел", "на стопе"]
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def lc(day: int) -> datetime:
    return datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc)


def test_effective_last_contact_takes_max():
    # в таблице 15.08, ученик писал 19.08 → берём 19.08
    got = effective_last_contact(date(2026, 8, 15), "2026-08-19T09:00:00+00:00", MSK)
    assert got == datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)
    # сообщений нет → дата из таблицы, конец дня по МСК
    got2 = effective_last_contact(date(2026, 8, 15), None, MSK)
    assert got2.astimezone(MSK).date() == date(2026, 8, 15)
    assert effective_last_contact(None, None, MSK) is None


def test_is_stopped_substring_case_insensitive():
    assert is_stopped("ОФФЕР ПРИНЯТ", STOP)
    assert is_stopped("Пока гошка на стопе", STOP)
    assert not is_stopped("Собесы", STOP)
    assert not is_stopped(None, STOP)


def test_send_window():
    assert in_send_window(datetime(2026, 8, 20, 11, 0, tzinfo=MSK), 11, 20)
    assert not in_send_window(datetime(2026, 8, 20, 20, 0, tzinfo=MSK), 11, 20)
    assert not in_send_window(datetime(2026, 8, 20, 3, 0, tzinfo=MSK), 11, 20)


def kw(**over):
    base = dict(
        last_contact=lc(15), status="Собесы", now_utc=NOW, stop_list=STOP,
        interval_days=3, unanswered=0, max_unanswered=3, paused_until_iso=None,
    )
    base.update(over)
    return base


def test_should_ping_matrix():
    assert should_ping(**kw())                                   # 5 дней тишины
    assert not should_ping(**kw(last_contact=lc(19)))            # писал вчера
    assert not should_ping(**kw(status="ОФФЕР ПРИНЯТ"))          # стоп-лист
    assert not should_ping(**kw(unanswered=3))                   # игнорит → эскалация
    assert not should_ping(**kw(paused_until_iso="2026-09-01T00:00:00+00:00"))
    assert should_ping(**kw(paused_until_iso="2026-08-01T00:00:00+00:00"))  # пауза истекла
    assert not should_ping(**kw(last_contact=None))              # нет данных


def test_naive_iso_treated_as_utc():
    got = effective_last_contact(None, "2026-08-19T09:00:00", MSK)
    assert got == datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)


def test_naive_pause_does_not_crash():
    assert not should_ping(**kw(paused_until_iso="2099-01-01T00:00:00"))
