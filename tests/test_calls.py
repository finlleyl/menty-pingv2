from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mentor_bot.calls import (
    BY_HAND, MORNING_KEY, calls_cycle, calls_morning, call_topic, handle_call, morning_due,
    morning_text, parse_call_args, parse_hhmm, resolve_slot, week_text, worth_checking,
)
from mentor_bot.llm import LLM, CallUpdate, LLMUnavailable
from mentor_bot.routers.callbacks import handle_call_callback
from mentor_bot.service import Service
from mentor_bot.store.repo import Repo
from tests.test_llm import FakeClient
from tests.test_service import FakeKB, FakeLLM, FakeSender, FakeSheets, sm

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)   # чт 08.10, 15:00 МСК


class Cfg:
    active_sheet_titles = ["A"]
    tz_name = "Europe/Moscow"
    debounce_minutes = 5
    calls_hour = 9


def upd(action="scheduled", date=None, time=None, kind=None, sprint=None, quote=""):
    return CallUpdate(action=action, date=date, time=time, kind=kind, sprint=sprint, quote=quote)


NONE = upd("none")


class CallLLM(FakeLLM):
    """Ответы extract_call по очереди; исключение в очереди — бросается."""

    def __init__(self, *updates):
        super().__init__()
        self.updates = list(updates)
        self.call_calls = []

    async def extract_call(self, earlier, new, *, now_local, status=None, booked=None):
        self.call_calls.append({"earlier": [m["text"] for m in earlier],
                                "new": [m["text"] for m in new], "status": status,
                                "booked": booked, "now_local": now_local})
        out = self.updates.pop(0) if self.updates else NONE
        if isinstance(out, Exception):
            raise out
        return out


class CardSender(FakeSender):
    def __init__(self):
        super().__init__()
        self.markups = []           # (message_id карточки, клавиатура)

    async def set_card_markup(self, msg_id, markup):
        self.markups.append((msg_id, markup))


def buttons(markup) -> list[str]:
    return [b.callback_data or b.url for row in markup.inline_keyboard for b in row]


async def make(tmp_path, *updates, mentees=None):
    repo = await Repo.open(str(tmp_path / "t.db"))
    llm = CallLLM(*updates)
    sender = CardSender()
    svc = Service(repo, FakeSheets(mentees or [sm()]), llm, sender, FakeKB(), Cfg())
    await svc.sync_mentees()
    return repo, svc, llm, sender


async def say(repo, direction, text, at, username="ivan"):
    await repo.log_message(username, direction, text, at.isoformat())


def at_msk(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK).astimezone(timezone.utc)


# --- чистые функции ---------------------------------------------------------------------------

def test_prefilter_calls_the_model_only_when_a_time_is_being_settled():
    asked = ["Когда удобно собес по спринту?"]
    assert worth_checking(["давай завтра в 19"], asked, booked=False)
    assert worth_checking(["го"], ["собес в чт в 19?"], booked=False)       # согласие на время
    assert not worth_checking(["го"], ["как дела?"], booked=False)          # не о созвоне
    assert not worth_checking(["как работает select?"], ["собес в чт в 19?"], booked=False)
    assert not worth_checking(["завтра доделаю задачу"], [], booked=False)   # время без созвона
    # записанный созвон меняют только новым временем или отменой
    assert not worth_checking(["👍"], ["собес в чт в 19?", "го"], booked=True)
    assert worth_checking(["не смогу, сорян"], [], booked=True)
    assert worth_checking(["давай в субботу в 12"], [], booked=True)


def test_time_and_slot_are_checked_by_code():
    assert parse_hhmm("19:00").hour == 19 and parse_hhmm("9.30").minute == 30
    assert parse_hhmm("19").hour == 19
    assert parse_hhmm("24:00") is None and parse_hhmm("вечером") is None
    assert resolve_slot("2026-10-09", "19:00", MSK, NOW) == at_msk(9, 19)
    assert resolve_slot("2026-10-08", "14:00", MSK, NOW) == at_msk(8, 14)   # идёт прямо сейчас
    assert resolve_slot("2026-10-07", "19:00", MSK, NOW) is None            # прошло
    assert resolve_slot("2027-06-01", "19:00", MSK, NOW) is None            # слишком далеко
    assert resolve_slot("09.10", "19:00", MSK, NOW) is None
    assert resolve_slot("2026-10-09", None, MSK, NOW) is None


def test_sprint_number_comes_from_chat_or_from_the_sheet():
    assert call_topic("sprint", None, "sprint2") == ("sprint", 2)
    assert call_topic("sprint", 4, "sprint2") == ("sprint", 4)      # назвали в переписке
    assert call_topic("sprint", 7, "sprint2") == ("sprint", 2)      # спринтов всего 4
    assert call_topic("sprint", None, "unknown") == ("sprint", None)
    assert call_topic(None, None, "sprint1") == ("sprint", 1)
    assert call_topic(None, None, "legend") == ("mock", None)
    assert call_topic(None, None, "unknown") == ("other", None)
    assert call_topic("mock", 3, "sprint1") == ("mock", None)


def test_manual_call_arguments():
    r = parse_call_args("@Ivan 15.10 19:00 спринт 2", NOW, MSK)
    assert (r.username, r.at, r.kind, r.sprint) == ("ivan", at_msk(15, 19), "sprint", 2)
    assert parse_call_args("ivan чт в 19", NOW, MSK).at == at_msk(8, 19)    # сегодня, ещё впереди
    assert parse_call_args("ivan чт 10:00", NOW, MSK).at == at_msk(15, 10)  # утро прошло — через неделю
    r = parse_call_args("ivan пятница 18:30 мок", NOW, MSK)
    assert (r.at, r.kind) == (at_msk(9, 18, 30), "mock")
    assert parse_call_args("ivan завтра 19", NOW, MSK).at == at_msk(9, 19)
    assert parse_call_args("ivan 05.01 19:00", NOW, MSK).at.year == 2027
    assert parse_call_args("ivan 15.10 19:00 2-й спринт", NOW, MSK).sprint == 2
    assert parse_call_args("ivan 15.10 19:00 по резюме", NOW, MSK).kind == "other"
    assert parse_call_args("ivan 15.10 19:00", NOW, MSK).kind is None
    assert parse_call_args("@ivan отмена", NOW, MSK).cancel
    with pytest.raises(ValueError, match="прошло"):
        parse_call_args("ivan сегодня 10:00", NOW, MSK)
    for bad in ("", "ivan", "ivan 31.02 19:00", "ivan 15.10", "ivan когда-нибудь 19:00"):
        with pytest.raises(ValueError, match="Формат"):
            parse_call_args(bad, NOW, MSK)


# --- разбор переписки -------------------------------------------------------------------------

async def test_agreement_in_chat_lands_in_the_calendar_with_the_sprint(tmp_path):
    repo, svc, llm, sender = await make(
        tmp_path, upd(date="2026-10-09", time="19:00", kind="sprint", quote="го"))
    await say(repo, "out", "Когда удобно собес по спринту?", NOW - timedelta(minutes=30))
    await say(repo, "in", "давай завтра в 19", NOW - timedelta(minutes=20))
    await say(repo, "out", "го", NOW - timedelta(minutes=10))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)

    [call] = llm.call_calls
    assert call["new"] == ["Когда удобно собес по спринту?", "давай завтра в 19", "го"]
    assert call["booked"] is None and call["status"] == "3 спринт"
    [c] = await repo.calls_between(NOW.isoformat(), (NOW + timedelta(days=7)).isoformat())
    assert (c["kind"], c["sprint"], c["starts_at"]) == ("sprint", 3, at_msk(9, 19).isoformat())
    text, kb = sender.mentor_msgs[-1]
    assert text == "📅 Записал в календарь: @ivan — собес по спринту 3\nпт 09.10 в 19:00\n«го»"
    assert f"call:del:{c['id']}" in buttons(kb) and "https://t.me/ivan" in buttons(kb)

    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW + timedelta(minutes=1))
    assert len(llm.call_calls) == 1          # переписка уже проверена — модель не зовём
    await repo.close()


async def test_chat_is_checked_only_after_it_quiets_down(tmp_path):
    repo, svc, llm, sender = await make(tmp_path, upd(date="2026-10-09", time="20:00"))
    await say(repo, "in", "собес завтра в 19 ок?", NOW - timedelta(minutes=3))
    await say(repo, "out", "давай в 20", NOW - timedelta(minutes=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert llm.call_calls == []
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW + timedelta(minutes=5))
    assert llm.call_calls[0]["new"] == ["собес завтра в 19 ок?", "давай в 20"]
    assert (await repo.upcoming_call("ivan", NOW.isoformat()))["starts_at"] == at_msk(9, 20).isoformat()
    await repo.close()


async def test_repeat_confirmation_reschedule_and_cancel(tmp_path):
    repo, svc, llm, sender = await make(
        tmp_path,
        upd(date="2026-10-09", time="19:00", kind="sprint"),
        upd(date="2026-10-09", time="19:00", kind="mock"),     # то же время — повтор
        upd(date="2026-10-10", time="12:00", kind="sprint", quote="давай в сб в 12"),
        upd("cancelled", quote="не смогу, заболел"),
    )
    t = NOW - timedelta(minutes=30)
    await say(repo, "out", "собес по спринту завтра в 19?", t)
    await say(repo, "in", "го", t + timedelta(minutes=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    first = await repo.upcoming_call("ivan", NOW.isoformat())

    await say(repo, "in", "ок, до завтра в 19", NOW + timedelta(hours=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW + timedelta(hours=2))
    assert len(sender.mentor_msgs) == 1                      # ничего не поменялось — тишина
    assert llm.call_calls[1]["booked"] == "пт 09.10 в 19:00, собес по спринту 3"
    assert llm.call_calls[1]["new"] == ["ок, до завтра в 19"]
    assert llm.call_calls[1]["earlier"][-1] == "го"

    await say(repo, "in", "слушай, давай в сб в 12", NOW + timedelta(hours=3))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW + timedelta(hours=4))
    moved = await repo.upcoming_call("ivan", NOW.isoformat())
    assert moved["id"] == first["id"] and moved["starts_at"] == at_msk(10, 12).isoformat()
    assert sender.mentor_msgs[-1][0] == ("📅 Перенёс: @ivan — собес по спринту 3\n"
                                         "пт 09.10 в 19:00 → сб 10.10 в 12:00\n«давай в сб в 12»")
    assert (first["card_msg_id"], "➡️ Изменён — карточка ниже") in sender.closed_cards

    await say(repo, "in", "не смогу, заболел", NOW + timedelta(hours=5))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW + timedelta(hours=6))
    assert await repo.upcoming_call("ivan", NOW.isoformat()) is None
    text, kb = sender.mentor_msgs[-1]
    assert text.startswith("🗑 Убрал из календаря: @ivan — сб 10.10 в 12:00, собес по спринту 3")
    assert f"call:back:{first['id']}" in buttons(kb)
    await repo.close()


async def test_model_is_not_called_without_talk_about_a_call(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    await say(repo, "in", "а почему тут дедлок?", NOW - timedelta(hours=1))
    await repo.upsert_mentee("stranger")                     # чат не из таблицы — как в роутере
    await say(repo, "out", "собес завтра в 19, го", NOW - timedelta(minutes=50), username="stranger")
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert llm.call_calls == []
    assert await repo.calls_unseen() == []                   # отметка сдвинулась
    await repo.close()


async def test_watermark_never_creates_a_mentee_row(tmp_path):
    # иначе бизнес-роутер не спросил бы «Новый чат — добавить?» про этого человека
    repo, svc, llm, sender = await make(tmp_path)
    await say(repo, "in", "привет", NOW - timedelta(hours=1), username="newbie")
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert await repo.get_mentee("newbie") is None
    await repo.close()


async def test_company_interviews_are_not_mentor_calls(tmp_path):
    repo, svc, llm, sender = await make(tmp_path, mentees=[sm(status="Собесы")])
    await say(repo, "in", "собес с Яндексом завтра в 19, ок", NOW - timedelta(hours=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert llm.call_calls == []
    await repo.close()


async def test_unusable_slot_from_the_model_is_not_booked(tmp_path):
    repo, svc, llm, sender = await make(tmp_path, upd(date="2026-10-07", time="19:00"),
                                        upd(date="2026-10-09", time="вечером"))
    await say(repo, "in", "собес вчера в 19, го", NOW - timedelta(hours=2))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    await say(repo, "in", "давай собес завтра вечером", NOW - timedelta(hours=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert len(llm.call_calls) == 2
    assert await repo.upcoming_call("ivan", NOW.isoformat()) is None
    assert sender.mentor_msgs == []
    await repo.close()


async def test_llm_outage_keeps_the_chat_for_the_next_tick(tmp_path):
    repo, svc, llm, sender = await make(tmp_path, LLMUnavailable("502"),
                                        upd(date="2026-10-09", time="19:00"))
    await say(repo, "in", "собес завтра в 19, ок", NOW - timedelta(hours=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert [r["username"] for r in await repo.calls_unseen()] == ["ivan"]
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert await repo.upcoming_call("ivan", NOW.isoformat()) is not None
    await repo.close()


async def test_broken_check_is_not_retried_forever_and_alerts_once(tmp_path):
    repo, svc, llm, sender = await make(tmp_path, ValueError("bad json"))
    await say(repo, "in", "собес завтра в 19, ок", NOW - timedelta(hours=1))
    await calls_cycle(svc, repo, llm, sender, Cfg(), now_utc=NOW)
    assert await repo.calls_unseen() == []
    assert any("договорённости о созвонах" in t for t, _ in sender.mentor_msgs)
    await repo.close()


# --- сводки ------------------------------------------------------------------------------------

async def _book(repo, username, at, kind="sprint", sprint=3, state="active"):
    cid = await repo.add_call(username, at.isoformat(), kind, sprint, BY_HAND, None,
                              NOW.isoformat())
    if state != "active":
        await repo.set_call_state(cid, state, NOW.isoformat())
    return cid


async def test_morning_digest_shows_today_and_tomorrow_once_a_day(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    await _book(repo, "ivan", at_msk(8, 19))
    await _book(repo, "petr", at_msk(9, 10), kind="mock", sprint=None)
    await _book(repo, "anna", at_msk(9, 9), state="cancelled")
    await _book(repo, "olga", at_msk(10, 12))                # послезавтра — не сюда
    morning = at_msk(8, 9, 2)
    assert await morning_text(repo, Cfg(), morning) == (
        "📅 Созвоны\nСегодня, чт 08.10:\n• 19:00 @ivan — собес по спринту 3\n"
        "Завтра, пт 09.10:\n• 10:00 @petr — мок-собес"
    )
    await calls_morning(repo, sender, Cfg(), now_utc=morning)
    await calls_morning(repo, sender, Cfg(), now_utc=morning + timedelta(minutes=1))
    assert len(sender.mentor_msgs) == 1                      # догонялка не задваивает
    assert not await morning_due(repo, Cfg(), morning + timedelta(hours=3))
    # назавтра бот лежал в 9:02 и поднялся в 11 — сводку догоняем; в 7 утра ещё рано
    assert await morning_due(repo, Cfg(), at_msk(9, 11))
    assert not await morning_due(repo, Cfg(), at_msk(9, 7))
    await repo.close()


async def test_empty_morning_is_silent(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    await _book(repo, "ivan", at_msk(12, 19))
    await calls_morning(repo, sender, Cfg(), now_utc=at_msk(8, 9, 2))
    assert sender.mentor_msgs == []
    assert await repo.get_setting(MORNING_KEY) == "2026-10-08"
    await repo.close()


async def test_week_lists_calls_by_day(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    await _book(repo, "petr", at_msk(8, 20), kind="mock", sprint=None)
    await _book(repo, "ivan", at_msk(8, 19))
    await _book(repo, "olga", at_msk(13, 11), kind="other", sprint=None)
    await _book(repo, "anna", at_msk(16, 12))                # через 8 дней
    text = await week_text("", repo, Cfg(), NOW)
    assert text == (
        "📅 Созвоны на 7 дн.:\n\nЧт 08.10 — сегодня\n• 19:00 @ivan — собес по спринту 3\n"
        "• 20:00 @petr — мок-собес\n\nВт 13.10\n• 11:00 @olga — созвон"
    )
    assert "@anna" in await week_text("10", repo, Cfg(), NOW)
    assert "созвонов нет" in await week_text("", repo, Cfg(), NOW + timedelta(days=9))
    await repo.close()


async def test_long_week_fits_into_one_telegram_message(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    for day in range(31):
        for hour in range(9, 22):
            await _book(repo, f"mentee_with_long_name_{hour}",
                        datetime(2026, 10, 8, hour, tzinfo=MSK).astimezone(timezone.utc)
                        + timedelta(days=day))
    text = await week_text("99", repo, Cfg(), NOW)
    assert text.startswith("📅 Созвоны на 31 дн.:") and len(text) <= 4096
    assert text.endswith("попроси /week на меньше дней")
    await repo.close()


# --- команда /call и кнопки --------------------------------------------------------------------

async def test_manual_call_books_fixes_and_cancels(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    assert await handle_call("@ivan 15.10 19:00", svc, repo, sender, NOW) is None
    text, kb = sender.mentor_msgs[-1]
    assert text == "📅 Записал в календарь: @ivan — собес по спринту 3\nчт 15.10 в 19:00"
    assert await handle_call("ivan 15.10 19:00", svc, repo, sender, NOW) == "Уже записано ровно так"
    assert await handle_call("ivan 15.10 19:00 мок", svc, repo, sender, NOW) is None
    assert sender.mentor_msgs[-1][0] == "📅 Поправил: @ivan — мок-собес\nчт 15.10 в 19:00"
    assert await handle_call("ivan отмена", svc, repo, sender, NOW) is None
    assert sender.mentor_msgs[-1][0].startswith("🗑 Убрал из календаря: @ivan")
    assert "нет записанного" in await handle_call("ivan отмена", svc, repo, sender, NOW)
    assert await handle_call("petya 15.10 19:00", svc, repo, sender, NOW) == "@petya нет в таблице"
    assert "Формат" in await handle_call("", svc, repo, sender, NOW)
    await repo.close()


async def test_delete_and_restore_buttons(tmp_path):
    repo, svc, llm, sender = await make(tmp_path)
    future = datetime.now(timezone.utc) + timedelta(days=2)
    cid = await _book(repo, "ivan", future)
    assert await handle_call_callback(f"call:del:{cid}", repo, sender, svc, card=77) == "Убрал из календаря"
    assert (await repo.get_call(cid))["state"] == "cancelled"
    assert sender.markups[-1][0] == 77 and f"call:back:{cid}" in buttons(sender.markups[-1][1])

    other = await _book(repo, "ivan", future + timedelta(days=1))
    assert "другой созвон" in await handle_call_callback(f"call:back:{cid}", repo, sender, svc, 77)
    await repo.set_call_state(other, "cancelled", NOW.isoformat())
    assert await handle_call_callback(f"call:back:{cid}", repo, sender, svc, 77) == "Вернул в календарь"
    assert (await repo.get_call(cid))["state"] == "active"
    assert f"call:del:{cid}" in buttons(sender.markups[-1][1])

    past = await _book(repo, "petr", datetime.now(timezone.utc) - timedelta(days=1),
                       state="cancelled")
    assert "прошло" in await handle_call_callback(f"call:back:{past}", repo, sender, svc, 78)
    await repo.close()


# --- запрос к модели и миграция ----------------------------------------------------------------

async def test_extract_call_prompt_has_calendar_and_dated_lines():
    fake = FakeClient([upd(date="2026-10-09", time="19:00")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    earlier = [{"direction": "out", "text": "собес по спринту завтра?",
                "ts": "2026-10-07T18:14:00+00:00"}]
    new = [{"direction": "in", "text": "го, в 19", "ts": "2026-10-08T11:50:00+00:00"}]
    out = await llm.extract_call(earlier, new, now_local=NOW.astimezone(MSK), status="Спринт 2")
    assert out.date == "2026-10-09"
    call = fake.chat.completions.calls[0]
    assert call["model"] == "smart"
    user = call["messages"][1]["content"]
    assert "Сейчас: чт 08.10 15:00, часовой пояс ментора Europe/Moscow" in user
    assert "Уже записан созвон: нет" in user and "«Спринт 2»" in user
    assert "ср 07.10 → 2026-10-07\n" in user                 # календарь — с первой реплики
    assert "пт 09.10 → 2026-10-09 (завтра)" in user and "чт 29.10 → 2026-10-29" in user
    assert "[ср 07.10 21:14] Ментор: собес по спринту завтра?" in user
    assert user.endswith("НОВЫЕ сообщения:\n[чт 08.10 14:50] Ученик: го, в 19")


async def test_old_database_gets_the_calls_watermark(tmp_path):
    import aiosqlite
    path = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(path)
    await conn.execute("CREATE TABLE mentees(username TEXT PRIMARY KEY, chat_id INTEGER, "
                       "sheet_title TEXT, row INTEGER, paused_until TEXT, "
                       "unanswered_pings INTEGER NOT NULL DEFAULT 0)")
    await conn.execute("INSERT INTO mentees(username) VALUES ('ivan')")
    await conn.execute("PRAGMA user_version=5")
    await conn.commit()
    await conn.close()
    repo = await Repo.open(path)
    await repo.log_message("ivan", "in", "привет", NOW.isoformat())
    [row] = await repo.calls_unseen()
    assert row["seen_id"] == 0
    await repo.set_calls_seen("ivan", row["max_id"])
    assert await repo.calls_unseen() == []
    await repo.close()
