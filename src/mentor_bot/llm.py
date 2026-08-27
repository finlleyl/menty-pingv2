from typing import Literal

from pydantic import BaseModel

from mentor_bot.stages import STAGE_LABELS, parse_stage, ping_topics


class Classification(BaseModel):
    kind: Literal["question", "progress", "other"]


class StatusUpdate(BaseModel):
    new_status: str | None
    confidence: Literal["high", "low"]


class PlainText(BaseModel):
    text: str


CLASSIFY_SYS = (
    "Ты сортируешь сообщения учеников ментора по Go-разработке. "
    "question — ученик задаёт вопрос, требующий ответа ментора (технический или организационный). "
    "progress — ученик сообщает о своём прогрессе (сдал спринт, прошёл собес, получил оффер, взял паузу). "
    "other — всё остальное (приветствия, окей, стикеры, болтовня)."
)

STATUS_SYS = (
    "Ученик написал сообщение. Текущий статус ученика в таблице ментора: «{current}». "
    "Определи, следует ли из сообщения НОВЫЙ статус. Примеры статусов: «1 спринт»…«4 спринт», "
    "«Собесы», «Рынок», «оффер», «приостановил», «занят». "
    "new_status=null, если статус не меняется. confidence=high только если из сообщения "
    "однозначно следует смена статуса; иначе low."
)

PING_SYS = (
    "Ты пишешь ОТ ИМЕНИ ментора по Go-разработке короткий пинг ученику, который уже несколько дней "
    "не выходил на связь. Стиль: неформальный, дружеский, на «ты», 1-2 предложения, без смайлов-спама, "
    "без канцелярита. Не представляйся, не пиши 'как ментор'.\n"
    "Ученик сейчас на этапе: {stage_label}.\n"
    "Спрашивать МОЖНО только про это: {allowed}.\n"
    "ЗАПРЕЩЕНО спрашивать про: {forbidden}. Даже вскользь, даже одним словом, "
    "даже как вежливый дополнительный вопрос.\n"
    "Верни только текст сообщения."
)

DRAFT_SYS = (
    "Ты готовишь ментору по Go-разработке ЧЕРНОВИК ответа на вопрос ученика. "
    "Отвечай ТОЛЬКО на основе приложенных выдержек из материалов курса; если в материалах ответа нет — "
    "так и напиши в черновике ('в материалах нет, ответь сам'). Стиль: неформальный, на «ты», по делу. "
    "Верни только текст ответа."
)

PROFILE_SYS = (
    "Обнови краткое досье ученика (3-6 предложений): чем занимается, что обсуждали, договорённости, тон "
    "общения. Старое досье и свежая переписка ниже. Верни только текст досье."
)


def _dialog(recent: list[dict]) -> str:
    return "\n".join(f"{'Ученик' if m['direction'] == 'in' else 'Ментор'}: {m['text']}" for m in recent)


class LLM:
    def __init__(self, api_key, model_smart, model_fast, embed_model, client=None):
        if client is None:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key)
        self._c = client
        self.smart = model_smart
        self.fast = model_fast
        self.embed_model = embed_model

    async def _parse(self, model, system, user, schema):
        resp = await self._c.chat.completions.parse(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
        )
        return resp.choices[0].message.parsed

    async def classify(self, text: str) -> str:
        out: Classification = await self._parse(self.fast, CLASSIFY_SYS, text, Classification)
        return out.kind

    async def parse_status(self, text: str, current_status: str | None) -> StatusUpdate:
        return await self._parse(
            self.fast, STATUS_SYS.format(current=current_status or "нет"), text, StatusUpdate
        )

    async def gen_ping(self, display, status, recent, profile, notes=None) -> str:
        stage = parse_stage(status)
        allowed, forbidden = ping_topics(stage)
        user = (
            f"Заметки ментора: {notes or 'нет'}\n"
            f"Досье: {profile or 'нет'}\n"
            f"Последняя переписка:\n{_dialog(recent) or 'нет'}\n"
            f"Ученик: {display}"
        )
        out: PlainText = await self._parse(
            self.smart,
            PING_SYS.format(
                stage_label=STAGE_LABELS[stage],
                allowed="; ".join(allowed),
                forbidden="; ".join(forbidden),
            ),
            user,
            PlainText,
        )
        return out.text

    async def draft_answer(self, question, chunks, profile) -> str:
        ctx = "\n\n---\n\n".join(chunks) or "(материалы не найдены)"
        user = f"Вопрос ученика: {question}\n\nДосье: {profile or 'нет'}\n\nМатериалы курса:\n{ctx}"
        out: PlainText = await self._parse(self.smart, DRAFT_SYS, user, PlainText)
        return out.text

    async def update_profile(self, old, recent) -> str:
        user = f"Старое досье: {old or 'нет'}\n\nСвежая переписка:\n{_dialog(recent)}"
        out: PlainText = await self._parse(self.fast, PROFILE_SYS, user, PlainText)
        return out.text

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._c.embeddings.create(model=self.embed_model, input=texts)
        return [d.embedding for d in resp.data]
