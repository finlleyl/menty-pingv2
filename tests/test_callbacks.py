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


async def test_q_send_refuses_draft_with_mentor_marker(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "Закрывает отправитель. [срок - допиши сам]",
                                  "2026-08-19T10:00:00+00:00")
    out = await handle_q_callback(f"q:send:{qid}", repo, sender, svc)
    assert out == "В черновике осталась пометка [срок - допиши сам] - нажми ✏️ Править"
    assert sender.mentee_msgs == []
    assert (await repo.get_question(qid))["state"] == "open"     # «Править» по-прежнему работает


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

    async def boom(m, s, expected=None):
        raise RuntimeError("sheets down")

    sheets.set_status = boom
    out = await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    assert "Ошибка" in out
    assert (await repo.get_proposal(pid)) is not None   # proposal сохранён для повтора


async def test_confirming_status_stamps_status_since(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    pid = await repo.add_proposal("ivan", "Резюме")
    result = await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    assert "Резюме" in result
    assert sheets.statuses == [("ivan", "Резюме")]
    # момент смены статуса зафиксирован — от него считаются дни ожидания
    assert (await repo.get_mentee("ivan"))["status_since"] is not None


async def test_st_yes_stale_proposal_does_not_regress_status(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    # предложение считалось от «3 спринт», а в таблице с тех пор уже «4 спринт»
    pid = await repo.add_proposal("ivan", "Спринт 4", from_status="2 спринт")
    out = await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    assert "устарело" in out
    assert sheets.statuses == []
    assert await repo.get_proposal(pid) is None


async def test_st_yes_writes_history(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    pid = await repo.add_proposal("ivan", "Спринт 4", from_status="3 спринт")
    await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    hist = await repo.status_history("ivan")
    assert [(h["from_status"], h["to_status"], h["source"]) for h in hist] == [
        (None, "3 спринт", "initial"), ("3 спринт", "Спринт 4", "bot"),
    ]
    # повторная синхронизация видит в таблице тот же статус — дубля в истории нет
    svc.sheets.mentees[0].status = "Спринт 4"
    await svc.sync_mentees()
    assert len(await repo.status_history("ivan")) == 2


async def test_edit_flow_sends_mentor_text_and_remembers_it(tmp_path):
    from mentor_bot.routers.callbacks import handle_edit_text
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "как закрыть канал?", "черновик", "2026-08-19T10:00:00+00:00")
    assert await handle_edit_text("просто текст", repo, sender, svc) is None   # правку никто не ждёт
    assert await handle_q_callback(f"q:edit:{qid}", repo, sender, svc) == "Жду текст"
    out = await handle_edit_text("close(ch), но только со стороны отправителя", repo, sender, svc)
    assert "твой вариант" in out
    assert sender.mentee_msgs == [("ivan", "close(ch), но только со стороны отправителя")]
    q = await repo.get_question(qid)
    assert q["state"] == "sent" and q["final"].startswith("close(ch)")
    assert await repo.edit_examples() == [
        {"question": "как закрыть канал?", "draft": "черновик", "final": q["final"]}
    ]
    assert await handle_edit_text("ещё текст", repo, sender, svc) is None      # режим правки снят


async def test_edit_of_closed_question_sends_nothing(tmp_path):
    from mentor_bot.routers.callbacks import handle_edit_text
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    await handle_q_callback(f"q:edit:{qid}", repo, sender, svc)
    await repo.set_question_state(qid, "answered")     # тем временем ответил в чате сам
    assert "закрыт" in await handle_edit_text("текст", repo, sender, svc)
    assert sender.mentee_msgs == []


async def test_send_as_is_records_final(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    await handle_q_callback(f"q:send:{qid}", repo, sender, svc)
    assert (await repo.get_question(qid))["final"] == "черновик"
    assert await repo.edit_examples() == []        # не правка — в примеры стиля не идёт


async def test_forgotten_edit_expires(tmp_path):
    from mentor_bot.routers.callbacks import EDIT_KEY, handle_edit_text
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    await repo.set_setting(EDIT_KEY, f"q:{qid}:2026-08-19T10:00:00+00:00")   # давно
    out = await handle_edit_text("случайный текст", repo, sender, svc)
    assert "отменена" in out and sender.mentee_msgs == []
    assert (await repo.get_question(qid))["state"] == "open"


async def test_ping_draft_not_sent_after_pause(tmp_path):
    from mentor_bot.routers.callbacks import handle_p_callback
    from tests.test_commands import Cfg
    repo, sheets, sender, svc = await make(tmp_path)
    svc.settings = type("S", (Cfg,), {"max_unanswered_pings": 3})()
    pid = await repo.add_ping_draft("ivan", "куда пропал?", "2026-08-20T12:00:00+00:00")
    await repo.set_pause("ivan", "2099-01-01T00:00:00+00:00")
    out = await handle_p_callback(f"p:send:{pid}", repo, sender, svc)
    assert "нельзя" in out and sender.mentee_msgs == []


async def test_bot_sends_are_logged_with_their_source(tmp_path):
    # эхо этих отправок бизнес-роутер пропускает, поэтому в переписку их пишет сам бот
    from mentor_bot.routers.callbacks import handle_edit_text
    repo, sheets, sender, svc = await make(tmp_path)
    q1 = await repo.add_question("ivan", "вопрос", "черновик модели", "2026-08-19T10:00:00+00:00")
    q2 = await repo.add_question("ivan", "ещё вопрос", "черновик", "2026-08-19T10:01:00+00:00")
    await handle_q_callback(f"q:send:{q1}", repo, sender, svc)
    await handle_q_callback(f"q:edit:{q2}", repo, sender, svc)
    await handle_edit_text("мой вариант ответа", repo, sender, svc)
    rows = await repo._all("SELECT text, source FROM messages WHERE username='ivan' ORDER BY id")
    assert rows == [{"text": "черновик модели", "source": "bot"},
                    {"text": "мой вариант ответа", "source": "bot_edit"}]
    assert (await repo.get_question(q2))["state"] == "sent"     # первая отправка его не закрыла
    assert await repo.last_out_ts("ivan") is None               # и ручным ответом не считается


async def test_cards_show_the_outcome_instead_of_buttons(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    q1 = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    q2 = await repo.add_question("ivan", "ещё", "черновик 2", "2026-08-19T10:01:00+00:00")
    await repo.set_card("questions", q1, 501)
    await repo.set_card("questions", q2, 502)
    await handle_q_callback(f"q:send:{q1}", repo, sender, svc)
    await handle_q_callback(f"q:ign:{q2}", repo, sender, svc)
    assert sender.closed_cards[0][0] == 501 and sender.closed_cards[0][1].startswith("✅ Отправлено")
    assert sender.closed_cards[1] == (502, "🙈 Игнор")
    # старая кнопка на уже закрытой карточке убирается, а не отвечает «Уже обработано» вечно
    assert await handle_q_callback(f"q:send:{q2}", repo, sender, svc) == "Уже обработано"
    assert sender.closed_cards[2] == (502, "🙈 Игнор")


async def test_card_of_old_version_falls_back_to_the_pressed_message(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    qid = await repo.add_question("ivan", "вопрос", "черновик", "2026-08-19T10:00:00+00:00")
    await handle_q_callback(f"q:ign:{qid}", repo, sender, svc, card=777)
    assert sender.closed_cards == [(777, "🙈 Игнор")]


async def test_status_and_add_cards_are_closed(tmp_path):
    repo, sheets, sender, svc = await make(tmp_path)
    pid = await repo.add_proposal("ivan", "Собесы", "3 спринт")
    await repo.set_card("proposals", pid, 601)
    await handle_st_callback(f"st:yes:{pid}", repo, sender, svc)
    pid2 = await repo.add_proposal("ivan", "оффер", "Собесы")
    await handle_st_callback(f"st:no:{pid2}", repo, sender, svc, card=602)
    await handle_add_callback("add:skip:mama", repo, sender, svc, card=603)
    assert sender.closed_cards == [(601, "✅ «Собесы»"), (602, "❌ Статус не меняем"),
                                   (603, "🚫 Не менти")]
