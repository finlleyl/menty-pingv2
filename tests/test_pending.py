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
