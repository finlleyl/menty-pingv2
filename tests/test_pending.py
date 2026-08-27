import json

from mentor_bot.store.repo import Repo


async def test_buffer_accumulates_and_moves_window(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.buffer_incoming("ivan", "привет", "2026-08-27T10:00:00+00:00")
    await repo.buffer_incoming("ivan", "а вот ещё", "2026-08-27T10:00:30+00:00")
    row = await repo.get_pending("ivan")
    assert json.loads(row["texts"]) == ["привет", "а вот ещё"]
    assert row["last_in_ts"] == "2026-08-27T10:00:30+00:00"
    await repo.close()


async def test_touch_pending_extends_window_only_if_buffer_exists(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.touch_pending("ivan", "2026-08-27T10:00:00+00:00")
    assert await repo.get_pending("ivan") is None  # медиа без буфера ничего не создаёт

    await repo.buffer_incoming("ivan", "привет", "2026-08-27T10:00:00+00:00")
    await repo.touch_pending("ivan", "2026-08-27T10:02:00+00:00")
    row = await repo.get_pending("ivan")
    assert row["last_in_ts"] == "2026-08-27T10:02:00+00:00"
    assert json.loads(row["texts"]) == ["привет"]  # текст не добавился
    await repo.close()


async def test_drop_pending(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.buffer_incoming("ivan", "привет", "2026-08-27T10:00:00+00:00")
    await repo.drop_pending("ivan")
    assert await repo.get_pending("ivan") is None
    await repo.drop_pending("ivan")  # повторный вызов безопасен
    await repo.close()


async def test_mature_pending_selects_only_ripe(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.buffer_incoming("ivan", "давно", "2026-08-27T10:00:00+00:00")
    await repo.buffer_incoming("petr", "только что", "2026-08-27T10:09:00+00:00")
    ripe = await repo.mature_pending("2026-08-27T10:05:00+00:00")
    assert [r["username"] for r in ripe] == ["ivan"]
    await repo.close()


async def test_last_out_ts_ignores_incoming(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    assert await repo.last_out_ts("ivan") is None
    await repo.log_message("ivan", "in", "вопрос", "2026-08-27T10:00:00+00:00")
    assert await repo.last_out_ts("ivan") is None
    await repo.log_message("ivan", "out", "ответ", "2026-08-27T10:01:00+00:00")
    await repo.log_message("ivan", "in", "спасибо", "2026-08-27T10:02:00+00:00")
    assert await repo.last_out_ts("ivan") == "2026-08-27T10:01:00+00:00"
    await repo.close()


from datetime import datetime, timezone

from mentor_bot.jobs import drain_pending
from mentor_bot.service import Service
from tests.test_commands import Cfg
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSheets, sm


class Cfg3(Cfg):
    debounce_minutes = 5


NOW = datetime(2026, 8, 27, 10, 10, tzinfo=timezone.utc)


async def make_svc(tmp_path, kind="question"):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sender = FakeSender()
    svc = Service(repo, FakeSheets([sm()]), FakeLLM(kind), sender, FakeKB(), Cfg3())
    await svc.sync_mentees()
    return repo, sender, svc


async def test_drain_merges_texts_into_one_llm_call(tmp_path):
    repo, sender, svc = await make_svc(tmp_path)
    seen = []

    async def classify(text):
        seen.append(text)
        return "question"

    svc.llm.classify = classify
    await repo.buffer_incoming("ivan", "привет", "2026-08-27T10:00:00+00:00")
    await repo.buffer_incoming("ivan", "как работает select?", "2026-08-27T10:00:30+00:00")
    await drain_pending(svc, repo, sender, Cfg3(), now_utc=NOW)
    assert seen == ["привет\nкак работает select?"]     # один вызов, склеенный текст
    assert await repo.get_pending("ivan") is None


async def test_drain_skips_unripe_buffer(tmp_path):
    repo, sender, svc = await make_svc(tmp_path)
    await repo.buffer_incoming("ivan", "только что", "2026-08-27T10:08:00+00:00")
    await drain_pending(svc, repo, sender, Cfg3(), now_utc=NOW)
    assert await repo.get_pending("ivan") is not None   # окно ещё не закрылось
    assert await repo.open_questions() == []


async def test_drain_cancels_when_mentor_answered_in_between(tmp_path):
    repo, sender, svc = await make_svc(tmp_path)
    await repo.buffer_incoming("ivan", "вопрос", "2026-08-27T10:00:00+00:00")
    # ментор ответил уже после последнего входящего — черновик не нужен
    await repo.log_message("ivan", "out", "уже ответил", "2026-08-27T10:03:00+00:00")
    await drain_pending(svc, repo, sender, Cfg3(), now_utc=NOW)
    assert await repo.get_pending("ivan") is None
    assert await repo.open_questions() == []


async def test_drain_clears_buffer_even_when_handling_raises(tmp_path):
    repo, sender, svc = await make_svc(tmp_path)

    async def boom(text):
        raise RuntimeError("llm down")

    svc.llm.classify = boom
    await repo.buffer_incoming("ivan", "вопрос", "2026-08-27T10:00:00+00:00")
    await drain_pending(svc, repo, sender, Cfg3(), now_utc=NOW)
    # буфер снят, иначе сломанное сообщение дренажилось бы каждую минуту вечно
    assert await repo.get_pending("ivan") is None
    assert any("⚠️" in m[0] for m in sender.mentor_msgs)


async def test_drain_keeps_messages_that_arrived_during_processing(tmp_path):
    repo, sender, svc = await make_svc(tmp_path)
    original = svc.handle_buffered

    async def slow(username, text, ts):
        # пока идёт запрос в LLM, ученик дописал ещё одно сообщение
        await repo.buffer_incoming(username, "а ещё вопрос", "2026-08-27T10:09:00+00:00")
        return await original(username, text, ts)

    svc.handle_buffered = slow
    await repo.buffer_incoming("ivan", "вопрос", "2026-08-27T10:00:00+00:00")
    await drain_pending(svc, repo, sender, Cfg3(), now_utc=NOW)
    row = await repo.get_pending("ivan")
    assert row is not None                                    # новое сообщение не потеряно
    assert json.loads(row["texts"]) == ["а ещё вопрос"]        # обработанное убрано, дублей не будет
    assert row["last_in_ts"] == "2026-08-27T10:09:00+00:00"


async def test_drain_keeps_late_message_even_when_handling_raises(tmp_path):
    repo, sender, svc = await make_svc(tmp_path)

    async def boom(username, text, ts):
        await repo.buffer_incoming(username, "а ещё вопрос", "2026-08-27T10:09:00+00:00")
        raise RuntimeError("llm down")

    svc.handle_buffered = boom
    await repo.buffer_incoming("ivan", "вопрос", "2026-08-27T10:00:00+00:00")
    await drain_pending(svc, repo, sender, Cfg3(), now_utc=NOW)
    row = await repo.get_pending("ivan")
    # сломанное сообщение выброшено, свежее осталось — цикла не будет
    assert json.loads(row["texts"]) == ["а ещё вопрос"]
