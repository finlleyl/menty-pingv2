from mentor_bot.llm import LLM, Classification, StatusUpdate


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
