import asyncio
from datetime import datetime, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection
from aiogram.types import BusinessBotRights, BusinessConnection, Chat, Message, User

from mentor_bot.routers import business
from mentor_bot.routers.commands import status_text
from mentor_bot.store.repo import SRC_AUTO
from tests.test_service import make

MENTOR = 999
TS = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


def conn(conn_id="conn1", owner=MENTOR, enabled=True, can_reply=True):
    return BusinessConnection(
        id=conn_id, user=User(id=owner, is_bot=False, first_name="Ментор"), user_chat_id=owner,
        date=TS, is_enabled=enabled, rights=BusinessBotRights(can_reply=can_reply),
    )


class FakeBot:
    def __init__(self, connections=None, error=None):
        self.connections = connections or {}
        self.error = error
        self.lookups = []

    async def get_business_connection(self, conn_id):
        self.lookups.append(conn_id)
        await asyncio.sleep(0)       # отдать управление, как настоящий запрос
        if self.error:
            raise self.error
        return self.connections[conn_id]


def bmsg(text, from_id, bconn="conn1", msg_id=1, **kw):
    return Message(
        message_id=msg_id, date=TS,
        chat=Chat(id=111, type="private", username="ivan", first_name="Иван"),
        from_user=User(id=from_id, is_bot=False, first_name="x"),
        text=text, business_connection_id=bconn, **kw,
    )


def bad_request():
    return TelegramBadRequest(GetBusinessConnection(business_connection_id="x"),
                              "Bad Request: business connection not found")


async def router_for(tmp_path, bconn="conn1"):
    repo, sheets, sender, svc = await make(tmp_path)
    if bconn:
        await repo.set_setting("bconn", bconn)
    r = business.make_router(svc, repo, MENTOR)
    return repo, sender, svc, r.business_message.handlers[0].callback, r.business_connection.handlers[0].callback


async def test_echo_of_bot_send_is_not_a_manual_answer(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    qid = await repo.add_question("ivan", "а что с каналами?", "черновик", "2026-08-20T08:00:00+00:00")
    await repo.buffer_incoming("ivan", "и ещё вопрос", "2026-08-20T08:59:00+00:00")
    await repo.upsert_mentee("ivan", chat_id=111)
    await repo.bump_unanswered("ivan")
    echo = bmsg("Канал закрывает отправитель, получатель читает до закрытия", MENTOR,
                sender_business_bot=User(id=42, is_bot=True, first_name="bot"))
    await handle(echo, bot=FakeBot())
    # вопрос не закрыт, буфер с новым вопросом цел, счётчик игнора пингов не обнулён
    assert (await repo.get_question(qid))["state"] == "open"
    assert (await repo.get_pending("ivan"))["texts"] == ["и ещё вопрос"]
    assert (await repo.get_mentee("ivan"))["unanswered_pings"] == 1
    # и в переписку эхо не задвоилось — её пишет тот, кто отправлял
    assert await repo.recent_messages("ivan") == []


async def test_business_auto_reply_is_logged_but_does_not_drop_buffer(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    await repo.buffer_incoming("ivan", "как закрыть канал?", "2026-08-20T08:59:00+00:00")
    await handle(bmsg("Я сейчас не на связи, отвечу позже", MENTOR, is_from_offline=True), bot=FakeBot())
    assert (await repo.get_pending("ivan"))["texts"] == ["как закрыть канал?"]
    assert await repo.last_out_ts("ivan") is None         # «меня нет» — не ответ ментора
    rows = await repo._all("SELECT text, source FROM messages WHERE username='ivan'")
    assert rows == [{"text": "Я сейчас не на связи, отвечу позже", "source": SRC_AUTO}]


async def test_manual_mentor_message_still_counts(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-20T08:00:00+00:00")
    await handle(bmsg("ответил сам, без кнопки, своими словами и подробно", MENTOR), bot=FakeBot())
    assert (await repo.get_question(qid))["state"] == "answered"
    assert await repo.last_out_ts("ivan") == TS.isoformat()


async def test_empty_bconn_adopts_mentor_connection_and_processes_message(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path, bconn="")
    bot = FakeBot({"conn1": conn()})
    await handle(bmsg("привет, есть вопрос", 111), bot=bot)
    assert await repo.get_setting("bconn") == "conn1"
    assert (await repo.get_pending("ivan"))["texts"] == ["привет, есть вопрос"]
    assert (await repo.get_mentee("ivan"))["chat_id"] == 111
    assert any("восстановлено" in m for m, _ in sender.mentor_msgs)


async def test_stale_bconn_is_replaced_by_the_new_mentor_connection(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path, bconn="old")
    await handle(bmsg("привет", 111, bconn="new"), bot=FakeBot({"new": conn("new")}))
    assert await repo.get_setting("bconn") == "new"


async def test_burst_on_unknown_connection_asks_telegram_once(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path, bconn="")
    bot = FakeBot({"conn1": conn()})
    await asyncio.gather(*(handle(bmsg(f"сообщение {i}", 111, msg_id=i + 1), bot=bot)
                           for i in range(4)))
    assert bot.lookups == ["conn1"]
    assert sorted((await repo.get_pending("ivan"))["texts"]) == [f"сообщение {i}" for i in range(4)]
    assert sum("восстановлено" in m for m, _ in sender.mentor_msgs) == 1


async def test_foreign_connection_is_dropped_and_remembered(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path, bconn="")
    bot = FakeBot({"alien": conn("alien", owner=555)})
    await handle(bmsg("чужое", 111, bconn="alien"), bot=bot)
    await handle(bmsg("чужое ещё", 111, bconn="alien", msg_id=2), bot=bot)
    assert not await repo.get_setting("bconn")
    assert await repo.recent_messages("ivan") == []
    assert bot.lookups == ["alien"]                      # второй раз Telegram не спрашиваем
    assert sender.mentor_msgs == []


async def test_unverifiable_connection_alerts_mentor_once(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path, bconn="")
    bot = FakeBot(error=bad_request())
    await handle(bmsg("привет", 111), bot=bot)
    await handle(bmsg("ау", 111, msg_id=2), bot=bot)
    assert await repo.recent_messages("ivan") == []
    assert sum("не подтверждает" in m for m, _ in sender.mentor_msgs) == 1


async def test_disabling_old_connection_keeps_the_current_one(tmp_path):
    repo, sender, svc, _, on_connection = await router_for(tmp_path, bconn="new")
    await on_connection(conn("old", enabled=False))
    assert await repo.get_setting("bconn") == "new"
    await on_connection(conn("new", enabled=False))
    assert await repo.get_setting("bconn") == ""


async def test_connection_without_reply_right_warns_mentor(tmp_path):
    repo, sender, svc, _, on_connection = await router_for(tmp_path, bconn="")
    await on_connection(conn(can_reply=False))
    assert await repo.get_setting("bconn") == "conn1"
    assert any("нет права отвечать" in m for m, _ in sender.mentor_msgs)


async def test_startup_check_clears_dead_connection(tmp_path):
    repo, sender, svc, _, _ = await router_for(tmp_path)
    await business.check_connection(FakeBot(error=bad_request()), repo, sender, MENTOR)
    assert await repo.get_setting("bconn") == ""
    assert any("больше не действует" in m for m, _ in sender.mentor_msgs)


async def test_startup_check_keeps_connection_on_network_error(tmp_path):
    repo, sender, svc, _, _ = await router_for(tmp_path)
    await business.check_connection(FakeBot(error=OSError("timeout")), repo, sender, MENTOR)
    assert await repo.get_setting("bconn") == "conn1"
    assert sender.mentor_msgs == []


async def test_startup_check_accepts_live_connection(tmp_path):
    repo, sender, svc, _, _ = await router_for(tmp_path)
    await business.check_connection(FakeBot({"conn1": conn()}), repo, sender, MENTOR)
    assert await repo.get_setting("bconn") == "conn1"
    assert sender.mentor_msgs == []


async def test_status_shows_business_connection(tmp_path):
    class Cfg:
        tz_name = "Europe/Moscow"
        stop_status_list = []
        ping_interval_days = 3
        max_unanswered_pings = 3

    repo, sender, svc, _, _ = await router_for(tmp_path)
    assert "Telegram для бизнеса: подключён" in await status_text(svc, repo, Cfg(), TS)
    await repo.set_setting("bconn", "")
    assert "не подключён" in await status_text(svc, repo, Cfg(), TS)



async def test_redelivered_update_is_processed_once(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    msg = bmsg("как закрыть канал?", 111, msg_id=77)
    await handle(msg, bot=FakeBot())
    await handle(msg, bot=FakeBot())                  # Telegram прислал тот же апдейт ещё раз
    assert (await repo.get_pending("ivan"))["texts"] == ["как закрыть канал?"]
    assert len(await repo.recent_messages("ivan")) == 1


async def test_mentee_reply_carries_the_original_message(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    original = bmsg("Канал закрывает отправитель, получатель читает до закрытия", MENTOR, msg_id=10)
    await handle(bmsg("а почему именно отправитель?", 111, msg_id=11, reply_to_message=original),
                 bot=FakeBot())
    [row] = await repo.mature_pending("2026-08-21T00:00:00+00:00")
    assert row["replies"] == ["Ментор: «Канал закрывает отправитель, получатель читает до закрытия»"]
    assert row["tg_ids"] == [11]


async def test_quote_wins_over_the_whole_replied_message(tmp_path):
    from aiogram.types import TextQuote
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    original = bmsg("Первое: закрывает отправитель. Второе: nil-канал блокирует навсегда.", MENTOR,
                    msg_id=10)
    reply = bmsg("вот это не понял", 111, msg_id=11, reply_to_message=original,
                 quote=TextQuote(text="nil-канал блокирует навсегда", position=31))
    await handle(reply, bot=FakeBot())
    assert (await repo.mature_pending("2026-08-21T00:00:00+00:00"))[0]["replies"] == [
        "Ментор: «nil-канал блокирует навсегда»"]


async def test_mentor_reply_to_a_question_answers_only_that_question(tmp_path):
    repo, sender, svc, handle, _ = await router_for(tmp_path)
    q1 = await repo.add_question("ivan", "как закрыть канал?", "ч1", "2026-08-20T08:00:00+00:00",
                                 msg_ids=[5])
    q2 = await repo.add_question("ivan", "что такое контекст?", "ч2", "2026-08-20T08:30:00+00:00",
                                 msg_ids=[6])
    question = bmsg("как закрыть канал?", 111, msg_id=5)
    await handle(bmsg("закрывает только отправитель, получатель дочитывает буфер", MENTOR, msg_id=7,
                      reply_to_message=question), bot=FakeBot())
    assert (await repo.get_question(q1))["state"] == "answered"
    assert (await repo.get_question(q1))["final"].startswith("закрывает только отправитель")
    assert (await repo.get_question(q2))["state"] == "open"     # второй вопрос ждёт своего ответа
    assert (await repo.get_question(q2))["final"] is None
