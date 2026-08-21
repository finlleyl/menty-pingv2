from datetime import date

from mentor_bot.routers.callbacks import handle_add_callback, handle_q_callback, handle_st_callback
from mentor_bot.service import Service
from mentor_bot.sheets import SheetMentee
from mentor_bot.store.repo import Repo
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSettings, FakeSheets, sm


class SendingSender(FakeSender):
    def __init__(self, repo=None):
        super().__init__()
        self.repo = repo
        self.mentee_msgs = []

    async def is_dryrun(self) -> bool:
        if self.repo is None:
            return True
        return await self.repo.get_setting("dryrun", "1") == "1"

    async def send_to_mentee(self, username, text):
        if self.repo is not None and await self.repo.get_setting("dryrun", "1") == "1":
            await self.notify_mentor(f"[dry-run] → @{username}:\n{text}")
            return "dry"
        self.mentee_msgs.append((username, text))
        return "sent"


class ThrowingSender(FakeSender):
    async def send_to_mentee(self, username, text):
        raise RuntimeError("boom")


async def make(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    sheets = FakeSheets([sm()])
    sender = SendingSender()
    svc = Service(repo, sheets, FakeLLM(), sender, FakeKB(), FakeSettings())
    await svc.sync_mentees()
    return repo, sheets, sender, svc


async def test_q_send(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    out = await handle_q_callback(f"q:send:{qid}", repo, sender, svc)
    assert sender.mentee_msgs == [("ivan", "черновик")]
    assert (await repo.get_question(qid))["state"] == "sent"
    assert "Отправлено" in out


async def test_q_send_error_keeps_open(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    out = await handle_q_callback(f"q:send:{qid}", repo, ThrowingSender(), svc)
    assert out == "Ошибка отправки, попробуй ещё раз"
    assert (await repo.get_question(qid))["state"] == "open"


async def test_q_ignore(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    await handle_q_callback(f"q:ign:{qid}", repo, sender, svc)
    assert (await repo.get_question(qid))["state"] == "ignored"
    assert sender.mentee_msgs == []


async def test_st_yes(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    pid = await repo.add_proposal("ivan", "Собесы")
    await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    assert sheets.statuses == [("ivan", "Собесы")]
    assert await repo.get_proposal(pid) is None


async def test_add_to_sheet(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    appended = []
    sheets.append_mentee = lambda title, display: _record(appended, title, display)
    out = await handle_add_callback("add:0:newguy", repo, sender, svc)
    assert appended == [("A", "@newguy")]
    assert "A" in out


async def _record(acc, title, display):
    acc.append((title, display))


async def test_add_double_tap_no_duplicate(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    appended = []
    sheets.append_mentee = lambda title, display: _record(appended, title, display)
    await handle_add_callback("add:0:newguy", repo, sender, svc)
    # эмулируем, что после sync менти уже в таблице
    svc.by_username["newguy"] = sm(username="newguy")
    out = await handle_add_callback("add:0:newguy", repo, sender, svc)
    assert out == "Уже в таблице"
    assert len(appended) == 1


async def test_add_stale_button_index(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    assert await handle_add_callback("add:9:someone", repo, sender, svc) == "Кнопка устарела"


async def test_st_yes_sheet_failure_keeps_proposal(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    pid = await repo.add_proposal("ivan", "Собесы")

    async def boom(m, s):
        raise RuntimeError("sheets down")

    sheets.set_status = boom
    out = await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    assert "Ошибка" in out
    assert (await repo.get_proposal(pid)) is not None   # proposal сохранён для повтора
