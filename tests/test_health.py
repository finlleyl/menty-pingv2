from datetime import datetime, timedelta, timezone

from aiogram.types import BusinessBotRights, BusinessConnection, User

from mentor_bot.health import alert_once, due_catch_ups, health_text, tracked
from mentor_bot.service import Service
from mentor_bot.store.repo import Repo
from tests.test_commands import Cfg
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSheets, sm

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


async def test_tracked_job_logs_runs_and_alerts_once_per_failure(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sender = FakeSender()
    state = {"fail": True}

    async def job():
        if state["fail"]:
            raise RuntimeError("таблица недоступна")

    run = tracked("ping_cycle", job, repo, sender)
    await run()
    await run()                                       # та же ошибка — второй раз не пишем
    assert [t for t, _ in sender.mentor_msgs] == [
        "⚠️ Упала задача «пинги»: RuntimeError: таблица недоступна"]
    [r] = await repo.job_runs()
    assert r["last_error"] == "RuntimeError: таблица недоступна" and r["last_ok"] is None
    state["fail"] = False
    await run()
    [r] = await repo.job_runs()
    assert r["last_ok"] and await repo.get_setting("alert:job:ping_cycle") == ""
    await repo.close()


async def test_alert_once_repeats_only_after_the_problem_changes_or_goes_away(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sender = FakeSender()
    for text in ("A", "A", "B", "", "B"):
        await alert_once(repo, sender, "k", text)
    assert [t for t, _ in sender.mentor_msgs] == ["A", "B", "B"]
    await repo.close()


def test_catch_up_only_jobs_that_already_ran_and_missed_their_slot():
    runs = [
        {"job": "nightly_backup", "last_ok": (NOW - timedelta(hours=30)).isoformat()},
        {"job": "dossier_cycle", "last_ok": (NOW - timedelta(hours=10)).isoformat()},
        {"job": "digest_cycle", "last_ok": None},     # ещё ни разу — не догоняем
    ]
    assert due_catch_ups(runs, NOW) == ["nightly_backup"]


async def test_send_interrupted_by_restart_is_not_reopened_and_mentor_checks_chat(tmp_path):
    """Упали между «Отправить» и итогом: ушло ли — неизвестно. Вернуть в 'open' значило бы
    дать отправить второй раз; помечаем 'uncertain', карточку закрываем, ментору — список."""
    from mentor_bot.cards import UNCERTAIN_LABEL
    from mentor_bot.main import report_stuck
    repo = await Repo.open(str(tmp_path / "t.db"))
    qid = await repo.add_question("ivan", "вопрос", "черновик", NOW.isoformat())
    await repo.set_card("questions", qid, 501)
    pid = await repo.add_ping_draft("petr", "пинг", NOW.isoformat())
    assert await repo.claim("questions", qid) and await repo.claim("ping_drafts", pid)
    stuck = await repo.release_stuck()
    assert {(r["table"], r["id"]) for r in stuck} == {("questions", qid), ("ping_drafts", pid)}
    assert (await repo.get_question(qid))["state"] == "uncertain"
    assert (await repo.get_ping_draft(pid))["state"] == "uncertain"
    assert not await repo.claim("questions", qid)             # второй отправки не будет
    assert await repo.release_stuck() == []
    sender = FakeSender()
    await report_stuck(stuck, sender)
    assert (501, UNCERTAIN_LABEL) in sender.closed_cards
    [(text, _)] = sender.mentor_msgs
    assert "@ivan" in text and "@petr" in text and "проверь чат" in text
    await repo.close()


async def test_finish_in_flight_lets_started_work_complete():
    import asyncio
    from mentor_bot.main import finish_in_flight
    done = []

    async def sending():
        await asyncio.sleep(0.05)
        done.append("logged")

    task = asyncio.create_task(sending())
    await finish_in_flight(timeout=1)
    assert done == ["logged"] and task.done()


async def test_undelivered_alert_is_retried_next_time(tmp_path):
    """notify_mentor упал — алерт не запоминаем, иначе ментор так и не узнал бы о проблеме."""
    repo = await Repo.open(str(tmp_path / "t.db"))
    sender = FakeSender()
    real = sender.notify_mentor

    async def flaky(text, reply_markup=None):
        raise RuntimeError("telegram down")

    sender.notify_mentor = flaky
    await alert_once(repo, sender, "k", "A")
    assert await repo.get_setting("alert:k", "") == ""
    sender.notify_mentor = real
    await alert_once(repo, sender, "k", "A")
    assert [t for t, _ in sender.mentor_msgs] == ["A"]
    await repo.close()


class HealthBot:
    def __init__(self, conn=None):
        self.conn = conn

    async def get_business_connection(self, conn_id):
        return self.conn


async def test_health_text_answers_is_everything_working(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    svc = Service(repo, FakeSheets([sm()]), FakeLLM(), FakeSender(), FakeKB(), Cfg())
    svc.kb.chunks = ["a", "b"]
    await svc.sync_mentees()
    await repo.set_setting("bconn", "conn1")
    await repo.buffer_incoming("ivan", "вопрос", (NOW - timedelta(hours=1)).isoformat())
    await repo.log_message("ivan", "in", "вопрос", (NOW - timedelta(hours=1)).isoformat())
    await repo.add_question("ivan", "старый", "ч", (NOW - timedelta(hours=5)).isoformat())
    await tracked("drain_pending", _ok, repo, FakeSender())()
    conn = BusinessConnection(id="conn1", user=User(id=999, is_bot=False, first_name="M"),
                              user_chat_id=999, date=NOW, is_enabled=True,
                              rights=BusinessBotRights(can_reply=False))
    text = await health_text(svc, repo, Cfg(), HealthBot(conn), NOW)
    assert "Telegram для бизнеса: ⚠️ подключён, но нет права отвечать" in text
    assert "LLM: ✅ отвечает" in text
    assert "База знаний: ✅ фрагментов 2" in text
    assert "в буфере 1 сообщ. ⚠️ давно не разбирались" in text
    assert "черновиков без ответа 1 (старше 4 ч — 1)" in text
    assert "• разбор сообщений — ✅" in text and "• досье — ещё не запускалась" in text
    await repo.set_setting("alerted_llm_down", "1")
    assert "LLM: ❌ недоступен" in await health_text(svc, repo, Cfg(), HealthBot(conn), NOW)
    await repo.close()


async def _ok():
    return None
