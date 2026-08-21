from mentor_bot.sender import Sender
from mentor_bot.store.repo import Repo


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, business_connection_id=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "bconn": business_connection_id})


async def make(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.upsert_mentee("ivan", chat_id=111)
    await repo.set_setting("bconn", "conn1")
    return FakeBot(), repo


async def test_dryrun_goes_to_mentor(tmp_path):
    bot, repo = await make(tmp_path)
    s = Sender(bot, repo, mentor_user_id=42)
    assert await s.send_to_mentee("ivan", "привет") == "dry"
    assert bot.sent[0]["chat_id"] == 42 and bot.sent[0]["bconn"] is None
    assert "ivan" in bot.sent[0]["text"] and "привет" in bot.sent[0]["text"]


async def test_real_send_uses_business_connection(tmp_path):
    bot, repo = await make(tmp_path)
    await repo.set_setting("dryrun", "0")
    s = Sender(bot, repo, mentor_user_id=42)
    assert await s.send_to_mentee("ivan", "привет") == "sent"
    assert bot.sent[0] == {"chat_id": 111, "text": "привет", "bconn": "conn1"}


async def test_no_chat_and_pause_all(tmp_path):
    bot, repo = await make(tmp_path)
    await repo.set_setting("dryrun", "0")
    s = Sender(bot, repo, mentor_user_id=42)
    assert await s.send_to_mentee("nochat", "x") == "no_chat"
    await repo.set_setting("pause_all", "1")
    assert await s.send_to_mentee("ivan", "x") == "paused"
    assert bot.sent == []


async def test_no_bconn_fails_closed(tmp_path):
    repo = await Repo.open(str(tmp_path / "t.db"))
    await repo.upsert_mentee("ivan", chat_id=111)
    await repo.set_setting("dryrun", "0")
    bot = FakeBot()
    s = Sender(bot, repo, mentor_user_id=42)
    assert await s.send_to_mentee("ivan", "x") == "no_bconn"
    assert bot.sent == []
