from mentor_bot.llm import LLM, Classification, PlainText, StatusUpdate


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


async def test_classify_and_status():
    fake = FakeClient([
        Classification(kind="question"),
        StatusUpdate(new_status="Собесы", confidence="high"),
    ])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    assert await llm.classify("а как работает select?") == "question"
    upd = await llm.parse_status("прошел мок, вышел на рынок", "3 спринт")
    assert upd.new_status == "Собесы" and upd.confidence == "high"
    # классификация должна идти на быстрой модели
    assert fake.chat.completions.calls[0]["model"] == "fast"


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
    fake = FakeClient([Classification(kind="other")])
    llm = LLM("k", "smart", "fast", "emb", client=fake, base_url="https://openrouter.ai/api/v1")
    await llm.classify("спасибо")
    assert fake.chat.completions.calls[0]["extra_body"] == {
        "provider": {"require_parameters": True}, "usage": {"include": True},
    }


async def test_direct_provider_gets_no_openrouter_routing():
    fake = FakeClient([Classification(kind="other")])
    llm = LLM("k", "smart", "fast", "emb", client=fake)
    await llm.classify("спасибо")
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
        await _raising_llm(exc).classify("спасибо")


@pytest.mark.parametrize("exc", [
    _status_error(openai.BadRequestError, 400),
    _status_error(openai.PermissionDeniedError, 403),        # модерация OpenRouter — вина запроса
])
async def test_request_specific_errors_propagate_as_is(exc):
    with pytest.raises(type(exc)):
        await _raising_llm(exc).classify("спасибо")


async def test_usage_is_recorded_per_task():
    fake = FakeClient([Classification(kind="other")])
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
    await llm.classify("спасибо")
    assert seen == [("classify", "fast", 120, 5, 0.0003)]


async def test_broken_usage_sink_does_not_break_request():
    fake = FakeClient([Classification(kind="question")])

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
    assert await llm.classify("а как?") == "question"
