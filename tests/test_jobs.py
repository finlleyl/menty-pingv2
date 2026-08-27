# tests/test_jobs.py
from datetime import datetime, timedelta, timezone

import pytest

from mentor_bot import jobs as jobs_mod
from mentor_bot.jobs import ping_cycle, remind_cycle
from mentor_bot.service import Service
from mentor_bot.store.repo import Repo
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSheets, sm
from tests.test_callbacks import SendingSender
from tests.test_commands import Cfg


class Cfg2(Cfg):
    quiet_start_hour = 11
    quiet_end_hour = 20


NOON_UTC = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)   # 15:00 МСК — окно открыто
NIGHT_UTC = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)   # 04:00 МСК — окно закрыто


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # F5 добавляет asyncio.sleep(58) после реальной отправки — в тестах не ждём.
    async def instant(*_a, **_kw):
        return None

    monkeypatch.setattr(jobs_mod.asyncio, "sleep", instant)


class FailingSender(SendingSender):
    def __init__(self, repo, fail_usernames):
        super().__init__(repo)
        self.fail_usernames = set(fail_usernames)

    async def send_to_mentee(self, username, text):
        if username in self.fail_usernames:
            raise RuntimeError("boom")
        return await super().send_to_mentee(username, text)


async def make(tmp_path, mentees=None):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sheets = FakeSheets(mentees or [sm()])
    sender = SendingSender(repo)
    llm = FakeLLM()
    calls = []

    async def gen_ping(display, status, recent, profile, notes=None):
        calls.append(display)
        return f"ПИНГ[{display}]"

    llm.gen_ping = gen_ping
    llm.gen_ping_calls = calls
    svc = Service(repo, sheets, llm, sender, FakeKB(), Cfg2())
    return repo, sheets, sender, llm, svc


async def test_ping_sent_and_sheet_updated(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan", chat_id=1)
    await repo.set_setting("dryrun", "0")
    await repo.set_setting("bconn", "conn1")
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    assert sender.mentee_msgs == [("ivan", "ПИНГ[Иван @ivan]")]
    assert sheets.dates and sheets.dates[0][0] == "ivan"
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 1


async def test_no_ping_outside_window(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan", chat_id=1)
    await repo.set_setting("dryrun", "0")
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NIGHT_UTC)
    assert sender.mentee_msgs == []


async def test_dry_ping_does_not_touch_sheet(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan", chat_id=1)  # dryrun по умолчанию on
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    assert sheets.dates == []
    assert sender.mentee_msgs == []            # ушло ментору, не ученику
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 0


async def test_remind_cycle(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.add_question("ivan", "вопрос", "черновик", "2026-08-20T05:00:00+00:00")
    await remind_cycle(repo, sender, now_utc=NOON_UTC)  # прошло 7 часов
    assert any("вопрос" in m[0] for m in sender.mentor_msgs)
    await remind_cycle(repo, sender, now_utc=NOON_UTC)  # повторно не напоминает
    assert len([m for m in sender.mentor_msgs if "вопрос" in m[0]]) == 1


async def test_remind_cycle_respects_quiet_hours_when_settings_passed(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.add_question("ivan", "вопрос", "черновик", "2026-08-20T05:00:00+00:00")
    await remind_cycle(repo, sender, now_utc=NIGHT_UTC, settings=Cfg2())  # 04:00 МСК — тихо
    assert sender.mentor_msgs == []


async def test_ping_cycle_continues_after_send_error_and_alerts(tmp_path):
    mentees = [sm(username="ivan"), sm(username="petr")]
    repo, sheets, _sender, llm, svc = await make(tmp_path, mentees=mentees)
    await repo.upsert_mentee("ivan", chat_id=1)
    await repo.upsert_mentee("petr", chat_id=2)
    await repo.set_setting("dryrun", "0")
    await repo.set_setting("bconn", "conn1")
    sender = FailingSender(repo, {"ivan"})
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    # первый (ivan) упал, второй (petr) всё равно получил пинг
    assert [u for u, _ in sender.mentee_msgs] == ["petr"]
    assert any("ошиб" in m[0].lower() for m in sender.mentor_msgs)


async def test_ping_dedup_same_day(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan", chat_id=1)
    await repo.set_setting("dryrun", "0")
    await repo.set_setting("bconn", "conn1")
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    assert sender.mentee_msgs == [("ivan", "ПИНГ[Иван @ivan]")]
    # второй прогон в тот же день — дедуп, второй пинг не уходит
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC + timedelta(hours=2))
    assert sender.mentee_msgs == [("ivan", "ПИНГ[Иван @ivan]")]


async def test_no_chat_id_skips_generation_when_not_dry(tmp_path):
    repo, sheets, sender, llm, svc = await make(tmp_path)
    # "ivan" ни разу не привязан к chat_id
    await repo.set_setting("dryrun", "0")
    await repo.set_setting("bconn", "conn1")
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    assert llm.gen_ping_calls == []
    assert sender.mentee_msgs == []


async def test_missing_bconn_alerts_once_and_stops_cycle(tmp_path):
    mentees = [sm(username="ivan"), sm(username="petr")]
    repo, sheets, sender, llm, svc = await make(tmp_path, mentees=mentees)
    await repo.upsert_mentee("ivan", chat_id=1)
    await repo.upsert_mentee("petr", chat_id=2)
    await repo.set_setting("dryrun", "0")
    # bconn не установлен
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    assert llm.gen_ping_calls == []
    assert sender.mentee_msgs == []
    assert any("Business connection" in m[0] for m in sender.mentor_msgs)
    alerts = [m for m in sender.mentor_msgs if "Business connection" in m[0]]
    assert len(alerts) == 1
    assert await repo.get_setting("alerted_no_bconn") == "1"


async def test_ping_cycle_backfills_sheet_date_from_chat(tmp_path):
    from datetime import date

    repo, sheets, sender, llm, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan", chat_id=1)
    # в чате писали 19.08, а в таблице руками откатили на 10.08
    sheets.mentees[0].last_date = date(2026, 8, 10)
    await repo.log_message("ivan", "in", "я тут", "2026-08-19T10:00:00+00:00")
    await repo.set_setting("dryrun", "0")
    await ping_cycle(svc, repo, sender, llm, Cfg2(), now_utc=NOON_UTC)
    # таблица догнала телеграм, пинг не ушёл (связь 19.08 < 3 дней от 20.08)
    assert ("ivan", date(2026, 8, 19)) in sheets.dates
    assert sender.mentee_msgs == []


async def test_dossier_cycle_updates_only_stale_and_writes_sheet(tmp_path):
    from mentor_bot.jobs import dossier_cycle

    mentees = [sm(username="ivan"), sm(username="petr")]
    repo, sheets, sender, llm, svc = await make(tmp_path, mentees=mentees)
    sheets.dossiers = []

    async def set_dossier(m, text):
        sheets.dossiers.append((m.username, text))

    sheets.set_dossier = set_dossier
    seen = []

    async def update_profile(old, recent, notes=None):
        seen.append((old, notes))
        return "новое досье"

    llm.update_profile = update_profile

    await repo.log_message("ivan", "in", "привет", "2026-08-27T10:00:00+00:00")
    await repo.set_profile("petr", "старое", "2026-08-27T11:00:00+00:00")
    await repo.log_message("petr", "in", "привет", "2026-08-27T10:00:00+00:00")

    await dossier_cycle(svc, repo, llm, sender, Cfg2(), now_utc=NOON_UTC)
    assert sheets.dossiers == [("ivan", "новое досье")]     # petr не устарел
    assert await repo.get_profile("ivan") == "новое досье"


async def test_dossier_cycle_passes_mentor_notes(tmp_path):
    from mentor_bot.jobs import dossier_cycle

    mentee = sm(username="ivan")
    mentee.notes = "тянет медленно, нужен пинок"
    repo, sheets, sender, llm, svc = await make(tmp_path, mentees=[mentee])

    async def set_dossier(m, text):
        pass

    sheets.set_dossier = set_dossier
    seen = []

    async def update_profile(old, recent, notes=None):
        seen.append(notes)
        return "досье"

    llm.update_profile = update_profile
    await repo.log_message("ivan", "in", "привет", "2026-08-27T10:00:00+00:00")
    await dossier_cycle(svc, repo, llm, sender, Cfg2(), now_utc=NOON_UTC)
    assert seen == ["тянет медленно, нужен пинок"]


async def test_dossier_cycle_survives_sheet_error(tmp_path):
    from mentor_bot.jobs import dossier_cycle

    mentees = [sm(username="ivan"), sm(username="petr")]
    repo, sheets, sender, llm, svc = await make(tmp_path, mentees=mentees)
    written = []

    async def set_dossier(m, text):
        if m.username == "ivan":
            raise RuntimeError("sheets down")
        written.append(m.username)

    sheets.set_dossier = set_dossier

    async def update_profile(old, recent, notes=None):
        return "досье"

    llm.update_profile = update_profile
    await repo.log_message("ivan", "in", "привет", "2026-08-27T10:00:00+00:00")
    await repo.log_message("petr", "in", "привет", "2026-08-27T10:00:00+00:00")
    await dossier_cycle(svc, repo, llm, sender, Cfg2(), now_utc=NOON_UTC)
    assert written == ["petr"]                                  # цикл не остановился
    assert any("Досье" in m[0] for m in sender.mentor_msgs)
