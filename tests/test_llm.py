from mentor_bot.llm import LLM, PlainText, StatusUpdate, Triage

SMALLTALK = Triage(kinds=["smalltalk"], needs_reply=False, urgent=False, milestone="none")
TECH = Triage(kinds=["tech_question"], needs_reply=True, urgent=False, milestone="none")


class FakeCompletions:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)

        class Msg:
            parsed = payload

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]

        return Resp()


class FakeChat:
    def __init__(self, payloads):
        self.completions = FakeCompletions(payloads)


class FakeClient:
    def __init__(self, payloads):
        self.chat = FakeChat(payloads)


async def test_triage_and_status():
    fake = FakeClient([
        TECH,
        StatusUpdate(new_status="Собесы", confidence="high"),
    ])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    tri = await llm.triage("а как работает select?",
                           [{"direction": "out", "text": "как спринт?"}], "3 спринт")
    assert tri.kinds == ["tech_question"] and tri.needs_reply
    upd = await llm.parse_status("прошел мок, вышел на рынок", "3 спринт")
    assert upd.new_status == "Собесы" and upd.confidence == "high"
    # разбор должен идти на быстрой модели
    call = fake.chat.completions.calls[0]
    assert call["model"] == "fast"
    # сортировщик видит прошлую переписку и статус, а новые сообщения — отдельно
    user = call["messages"][1]["content"]
    assert "Ментор: как спринт?" in user and "«3 спринт»" in user
    assert user.rstrip().endswith("а как работает select?")
    system = call["messages"][0]["content"]
    for label in ("tech_question", "org_question", "feelings", "win", "status_change", "smalltalk",
                  "sprint_finished", "legend_ready", "urgent"):
        assert label in system


def _draft_prompt(fake):
    call = fake.chat.completions.calls[-1]
    return call["messages"][0]["content"], call["messages"][1]["content"]


async def test_draft_for_feelings_has_no_course_materials_and_forbids_calls():
    fake = FakeClient([PlainText(text="бывает")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    out = await llm.draft_reply(
        "чёт совсем руки опустились", ["feelings"], recent=[], stage_label="3-й спринт обучения",
        status="Спринт 3", notes="любит, когда по-простому", samples=["го глянем, скинь код"],
    )
    assert out == "бывает"
    system, user = _draft_prompt(fake)
    assert fake.chat.completions.calls[-1]["model"] == "smart"
    assert "Материалы курса" not in user and "Вопросов нет" in user
    assert "любит, когда по-простому" in user               # заметки ментора дошли
    assert "го глянем, скинь код" in user and "Так пишет ментор" in user
    assert "РЕГЛАМЕНТ СОЗВОНОВ" in system and "созвон НЕ предлагай" in system
    assert "«Не переживай»" in system                       # список штампов на месте
    assert "Привет" not in system.split("ЧТОБЫ НЕ ЗВУЧАТЬ")[0]   # диалог не идёт — не запрещаем


async def test_draft_call_rule_follows_permission_and_avoid_list():
    fake = FakeClient([PlainText(text="a"), PlainText(text="b"), PlainText(text="c")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    await llm.draft_reply("сдал 2 спринт!", ["win"], call="sprint", ongoing=True)
    system, _ = _draft_prompt(fake)
    assert "собеседования по спринту" in system and "не начинай с «Привет»" in system
    await llm.draft_reply("легенда готова", ["win"], call="mock")
    system, _ = _draft_prompt(fake)
    assert "мок-собеса по легенде" in system
    await llm.draft_reply("как закрыть канал?", ["tech_question"],
                          chunks=[{"text": "close(ch)", "source": "урок «Каналы»"}],
                          avoid=["предлагает созвон («созвон»)"])
    system, user = _draft_prompt(fake)
    assert "созвон НЕ предлагай" in system
    assert "[Источник: урок «Каналы»]" in user
    assert "не прошёл проверку: предлагает созвон" in user


async def test_gen_ping_gets_style_samples_and_call_ban():
    fake = FakeClient([PlainText(text="как оно?")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    await llm.gen_ping("Иван @ivan", "Спринт 2", [], None, samples=["ну чё, как каналы?"])
    system = fake.chat.completions.calls[0]["messages"][0]["content"]
    user = fake.chat.completions.calls[0]["messages"][1]["content"]
    assert "ну чё, как каналы?" in user
    assert "созвоны" in system.split("ЗАПРЕЩЕНО")[1].split("\n")[0]
    assert "ЧТОБЫ НЕ ЗВУЧАТЬ КАК НЕЙРОСЕТЬ" in system


async def test_gen_ping_injects_stage_gate():
    fake = FakeClient([PlainText(text="как спринт?")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    await llm.gen_ping("Иван @ivan", "Спринт 2", [], None)
    system = fake.chat.completions.calls[0]["messages"][0]["content"]
    # промпт должен явно запрещать собесы и рынок ученику со 2-го спринта
    assert "собеседован" in system.lower()
    assert "ЗАПРЕЩЕНО" in system
    assert "2-й спринт" in system
    # генерация пинга идёт на умной модели
    assert fake.chat.completions.calls[0]["model"] == "smart"


async def test_gen_ping_market_stage_forbids_sprint():
    fake = FakeClient([PlainText(text="как отклики?")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    await llm.gen_ping("Иван @ivan", "Поиск работы", [], None)
    system = fake.chat.completions.calls[0]["messages"][0]["content"]
    assert "активный поиск работы" in system
    forbidden_part = system.split("ЗАПРЕЩЕНО")[1]
    assert "спринт" in forbidden_part.lower()


from mentor_bot.llm import looks_like_verdict


def test_looks_like_verdict_matches_mentor_phrasing():
    assert looks_like_verdict("сдан спринт 1, красава")
    assert looks_like_verdict("Сдал! идёшь дальше")
    assert looks_like_verdict("принято, закрываю спринт")
    assert looks_like_verdict("проверил, всё ок")


def test_looks_like_verdict_ignores_ordinary_messages():
    assert not looks_like_verdict("привет, как дела?")
    assert not looks_like_verdict("посмотри вот это видео по DDD")
    assert not looks_like_verdict("давай созвон в четверг")


async def test_parse_mentor_verdict_uses_fast_model_and_current_status():
    fake = FakeClient([StatusUpdate(new_status="Спринт 2", confidence="high")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    upd = await llm.parse_mentor_verdict("сдан спринт 1", "Спринт 1")
    assert upd.new_status == "Спринт 2"
    call = fake.chat.completions.calls[0]
    assert call["model"] == "fast"
    system = call["messages"][0]["content"]
    assert "Спринт 1" in system            # текущий статус подставлен
    # весь конвейер после спринтов описан в промпте
    for stage in ("Резюме", "Легенда", "Мок", "Рынок"):
        assert stage in system


def test_looks_like_verdict_catches_pipeline_phrases():
    assert looks_like_verdict("резюме готово, кидаю")
    assert looks_like_verdict("скинул тебе резюме")
    assert looks_like_verdict("легенда готова")
    assert looks_like_verdict("мок прошли, идёшь на рынок")


def test_looks_like_verdict_still_ignores_chatter():
    assert not looks_like_verdict("напиши мне завтра")
    assert not looks_like_verdict("какой у тебя стек на проекте?")


import httpx
import openai
import pytest

from mentor_bot.llm import LLMUnavailable


def _status_error(cls, code):
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


class RaisingCompletions:
    def __init__(self, exc):
        self.exc = exc

    async def parse(self, **kwargs):
        raise self.exc


def _raising_llm(exc):
    fake = FakeClient([])
    fake.chat.completions = RaisingCompletions(exc)
    return LLM("k", "smart", "fast", "emb", client=fake)


async def test_openrouter_requires_hosts_that_support_the_schema():
    fake = FakeClient([SMALLTALK])
    llm = LLM("k", "smart", "fast", "emb", client=fake, base_url="https://openrouter.ai/api/v1")
    await llm.triage("спасибо")
    assert fake.chat.completions.calls[0]["extra_body"] == {
        "provider": {"require_parameters": True}, "usage": {"include": True},
    }


async def test_direct_provider_gets_no_openrouter_routing():
    fake = FakeClient([SMALLTALK])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    await llm.triage("спасибо")
    assert fake.chat.completions.calls[0]["extra_body"] is None


@pytest.mark.parametrize("exc", [
    _status_error(openai.AuthenticationError, 401),          # ключ отозван / аккаунт отключён
    _status_error(openai.APIStatusError, 402),               # кончились кредиты OpenRouter
    _status_error(openai.RateLimitError, 429),
    _status_error(openai.InternalServerError, 502),
    openai.APIConnectionError(request=httpx.Request("POST", "https://openrouter.ai")),
])
async def test_provider_outage_becomes_llm_unavailable(exc):
    with pytest.raises(LLMUnavailable):
        await _raising_llm(exc).triage("спасибо")


@pytest.mark.parametrize("exc", [
    _status_error(openai.BadRequestError, 400),
    _status_error(openai.PermissionDeniedError, 403),        # модерация OpenRouter — вина запроса
])
async def test_request_specific_errors_propagate_as_is(exc):
    with pytest.raises(type(exc)):
        await _raising_llm(exc).triage("спасибо")


async def test_usage_is_recorded_per_task():
    fake = FakeClient([SMALLTALK])
    seen = []

    async def sink(ts, task, model, pt, ct, cost):
        seen.append((task, model, pt, ct, cost))

    class Usage:
        prompt_tokens, completion_tokens, cost = 120, 5, 0.0003

    orig = fake.chat.completions.parse

    async def parse_with_usage(**kw):
        resp = await orig(**kw)
        resp.usage = Usage()
        return resp

    fake.chat.completions.parse = parse_with_usage
    llm = LLM("k", "smart", "fast", "emb", client=fake, usage_sink=sink)
    await llm.triage("спасибо")
    assert seen == [("triage", "fast", 120, 5, 0.0003)]


async def test_broken_usage_sink_does_not_break_request():
    fake = FakeClient([TECH])

    async def sink(*a):
        raise RuntimeError("db locked")

    llm = LLM("k", "smart", "fast", "emb", client=fake, usage_sink=sink)

    class Usage:
        prompt_tokens, completion_tokens = 1, 1

    orig = fake.chat.completions.parse

    async def parse_with_usage(**kw):
        resp = await orig(**kw)
        resp.usage = Usage()
        return resp

    fake.chat.completions.parse = parse_with_usage
    assert (await llm.triage("а как?")).kinds == ["tech_question"]
