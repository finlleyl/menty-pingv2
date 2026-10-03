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


async def test_close_card_replaces_buttons_and_survives_telegram_errors(tmp_path):
    bot, repo = await make(tmp_path)
    edits = []

    async def edit_message_reply_markup(chat_id, message_id, reply_markup):
        edits.append((chat_id, message_id, reply_markup))
        if message_id == 2:
            raise RuntimeError("message to edit not found")

    bot.edit_message_reply_markup = edit_message_reply_markup
    s = Sender(bot, repo, mentor_user_id=999)
    await s.close_card(1, "✅ Отправлено 14:32", "ivan")
    await s.close_card(2, "🙈 Игнор")                 # карточку удалили — не падаем
    await s.close_card(None, "🙈 Игнор")              # старая запись без карточки — no-op
    chat_id, msg_id, markup = edits[0]
    assert (chat_id, msg_id) == (999, 1)
    assert markup.inline_keyboard[0][0].text == "✅ Отправлено 14:32"
    assert markup.inline_keyboard[1][0].url == "https://t.me/ivan"
    assert len(edits) == 2


async def test_closed_business_window_is_a_result_not_an_error(tmp_path):
    import pytest
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage
    bot, repo = await make(tmp_path)
    await repo.set_setting("dryrun", "0")
    s = Sender(bot, repo, mentor_user_id=999)

    async def refuse(chat_id, text, business_connection_id=None, reply_markup=None):
        raise TelegramBadRequest(SendMessage(chat_id=chat_id, text=text),
                                 "Bad Request: BUSINESS_PEER_USAGE_MISSING")

    bot.send_message = refuse
    assert await s.send_to_mentee("ivan", "привет") == "window_closed"

    async def broken(chat_id, text, business_connection_id=None, reply_markup=None):
        raise TelegramBadRequest(SendMessage(chat_id=chat_id, text=text), "Bad Request: chat not found")

    bot.send_message = broken
    with pytest.raises(TelegramBadRequest):
        await s.send_to_mentee("ivan", "привет")
