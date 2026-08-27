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
    assert "Собесы" in system              # правило перехода после 4-го спринта
