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
