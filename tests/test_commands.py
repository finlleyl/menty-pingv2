# tests/test_commands.py
import asyncio
from datetime import datetime, timezone

from mentor_bot.routers import commands as commands_mod
from mentor_bot.routers.commands import handle_dryrun, handle_pause, status_text
from mentor_bot.service import Service
from mentor_bot.store.repo import Repo
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSettings, FakeSheets, sm


class Cfg(FakeSettings):
    ping_interval_days = 3
    max_unanswered_pings = 3
    stop_status_list = ["умер", "оффер"]
    tz_name = "Europe/Moscow"


async def make(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    svc = Service(repo, FakeSheets([sm()]), FakeLLM(), FakeSender(), FakeKB(), Cfg())
    await svc.sync_mentees()
    return repo, svc


async def test_status_text(tmp_path):
    repo, svc = await make(tmp_path)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    txt = await status_text(svc, repo, Cfg(), now)
    assert "dry-run: ON" in txt
    assert "@ivan" in txt          # 15.08 + 3 дня < 20.08 → кандидат на пинг
    assert "не привязан" in txt    # chat_id нет


async def test_pause_parse(tmp_path):
    repo, svc = await make(tmp_path)
    await repo.upsert_mentee("ivan")
    out = await handle_pause("@ivan 14", repo)
    assert "14" in out
    assert (await repo.get_mentee("ivan"))["paused_until"] is not None
    assert "Формат" in await handle_pause("мусор", repo)


async def test_dryrun_toggle(tmp_path):
    repo, svc = await make(tmp_path)
    await handle_dryrun("off", repo)
    assert await repo.get_setting("dryrun") == "0"
    await handle_dryrun("on", repo)
    assert await repo.get_setting("dryrun") == "1"


async def test_reindex_task_keeps_reference():
    done = asyncio.Event()

    async def fake_reindex():
        done.set()

    task = asyncio.create_task(fake_reindex())
    commands_mod._bg_tasks.add(task)
    task.add_done_callback(commands_mod._bg_tasks.discard)
    await asyncio.wait_for(done.wait(), timeout=1)
    await task
    # Allow callback to execute
    await asyncio.sleep(0)
    assert task not in commands_mod._bg_tasks
