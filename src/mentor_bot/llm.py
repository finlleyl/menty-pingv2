import logging
import re
from datetime import datetime, timezone
from typing import Literal

import openai
from pydantic import BaseModel

from mentor_bot.stages import STAGE_LABELS, parse_stage, ping_topics

log = logging.getLogger(__name__)


class Classification(BaseModel):
    kind: Literal["question", "progress", "other"]


class StatusUpdate(BaseModel):
    new_status: str | None
    confidence: Literal["high", "low"]


class PlainText(BaseModel):
    text: str


class InterviewItem(BaseModel):
    company: str | None
    stage: str | None          # «HR», «техничка», «лайвкодинг», «финал» — как назвал ученик
    question: str              # сам вопрос или задача, коротко
    failed: bool               # ученик сказал, что не ответил, поплыл, срезался


class InterviewReport(BaseModel):
    items: list[InterviewItem]


CLASSIFY_SYS = (
    "Ты сортируешь сообщения учеников ментора по Go-разработке. "
    "question — ученик задаёт вопрос, требующий ответа ментора (технический или организационный). "
    "progress — ученик сообщает о своём прогрессе (сдал спринт, прошёл собес, получил оффер, взял паузу). "
    "other — всё остальное (приветствия, окей, стикеры, болтовня)."
)

STATUS_SYS = (
    "Ученик написал сообщение. Текущий статус ученика в таблице ментора: «{current}». "
    "Определи, следует ли из сообщения НОВЫЙ статус. Допустимые статусы: «Спринт 1», "
    "«Спринт 2», «Спринт 3», «Спринт 4», «Резюме», «Легенда», «Мок», «Собесы», «Рынок», "
    "«оффер», «приостановил», «занят». "
    "Пиши статус ровно в этой форме. "
    "new_status=null, если статус не меняется. confidence=high только если из сообщения "
    "однозначно следует смена статуса; иначе low."
)

VERDICT_SYS = (
    "Ментор написал своему ученику сообщение. Текущий статус ученика в таблице: «{current}». "
    "Определи, следует ли из слов МЕНТОРА новый статус ученика. "
    "Конвейер обучения: Спринт 1 → Спринт 2 → Спринт 3 → Спринт 4 → «Резюме» (его пишет "
    "ментор) → «Легенда» (её пишет ученик) → «Мок» (мок-собес с ментором) → «Рынок». "
    "Правила перехода: «сдан спринт N» → «Спринт N+1» для N = 1, 2, 3; "
    "«сдан спринт 4» → «Резюме»; резюме готово или отправлено ученику → «Легенда»; "
    "легенда готова → «Мок»; мок сдан или пройден → «Рынок». "
    "new_status=null, если ментор не выносит вердикт по спринту. "
    "confidence=high только если вердикт однозначен."
)

# Дешёвый предфильтр: ментор пишет много, гонять модель на каждое сообщение нельзя
_VERDICT_RE = re.compile(
    r"сда(л|н|ла)\b|принят|зачт|провер(ил|ено)|закр(ыл|ываю) спринт"
    r"|резюме|легенд|\bмок",
    re.IGNORECASE,
)


def looks_like_verdict(text: str) -> bool:
    """Похоже ли сообщение ментора на вердикт по спринту."""
    return bool(_VERDICT_RE.search(text or ""))

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
    "Отвечай ТОЛЬКО на основе приложенных выдержек из материалов курса и прошлых ответов ментора "
    "на похожие вопросы; если ответа нет ни там, ни там — "
    "так и напиши в черновике ('в материалах нет, ответь сам'). Стиль: неформальный, на «ты», по делу. "
    "Если приложены примеры того, как ментор переписывал прошлые черновики, — пиши так, как он "
    "в итоге отправлял: та же длина, тон, манера. "
    "Если ответ взят из материалов, в конце одной строкой подскажи, где почитать подробнее, — "
    "по пометкам «Источник» у выдержек (например: «подробнее — урок „Каналы“»). "
    "Верни только текст ответа."
)

PROFILE_SYS = (
    "Обнови краткое досье ученика (3-6 предложений): чем занимается, что обсуждали, договорённости, тон "
    "общения. Личные пометки ментора об ученике — самый достоверный источник: не противоречь им и не "
    "смягчай их. Старое досье, пометки ментора и свежая переписка ниже. Верни только текст досье."
)


INTERVIEW_SYS = (
    "Ученик курса Go-разработки пишет ментору. Если он рассказывает о прошедшем собеседовании, "
    "выпиши КАЖДЫЙ конкретный вопрос или задачу, которые ему задавали, отдельным пунктом, "
    "коротко и по сути (например: «чем отличается буферизованный канал от небуферизованного»). "
    "failed=true, если ученик говорит, что не ответил, запутался, завалил или срезался на этом. "
    "company и stage — если названы, иначе null. Общие слова («было норм», «спрашивали про Go») "
    "пунктами не считаются. Если рассказа о собеседовании нет — items=[]."
)

# Предфильтр: разбирать фидбэк моделью стоит, только если в тексте хоть что-то про собес
_INTERVIEW_RE = re.compile(
    r"собес|интервью|скрин|техничк|лайвкод|фидб[эе]к|спрашивал|спросил|задач[аиуе]|тимлид|\bhr\b",
    re.IGNORECASE,
)


def looks_like_interview(text: str) -> bool:
    return bool(_INTERVIEW_RE.search(text or ""))


class LLMUnavailable(Exception):
    """Провайдер не отвечает: сеть, ключ, кредиты, лимиты, 5xx. Сообщение не виновато — повторить позже."""


# 402 — у OpenRouter кончились кредиты; 404 — нет модели/хоста под параметры, лечится конфигом.
# 400 и 403 (у OpenRouter это модерация входа) — проблема конкретного запроса, повтор не поможет.
_OUTAGE_STATUSES = {401, 402, 404, 408, 409, 429}


def _is_outage(e: Exception) -> bool:
    if isinstance(e, openai.APIConnectionError):
        return True
    if isinstance(e, openai.APIStatusError):
        return e.status_code in _OUTAGE_STATUSES or e.status_code >= 500
    return False


def _chunk_text(c) -> str:
    if isinstance(c, str):
        return c
    return f"[Источник: {c['source']}]\n{c['text']}" if c.get("source") else c["text"]


def _dialog(recent: list[dict]) -> str:
    return "\n".join(f"{'Ученик' if m['direction'] == 'in' else 'Ментор'}: {m['text']}" for m in recent)


class LLM:
    def __init__(self, api_key, model_smart, model_fast, embed_model, client=None, base_url=None,
                 usage_sink=None):
        # usage_sink(ts_iso, task, model, prompt_tokens, completion_tokens, cost) — учёт расходов
        self._usage_sink = usage_sink
        if client is None:
            client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self._c = client
        self.smart = model_smart
        self.fast = model_fast
        self.embed_model = embed_model
        # OpenRouter раздаёт модель нескольким хостам, structured outputs умеют не все —
        # без этого запрос со схемой может уехать туда, где схему проигнорируют
        # usage.include — OpenRouter возвращает в usage стоимость запроса в долларах
        self._route = (
            {"provider": {"require_parameters": True}, "usage": {"include": True}}
            if base_url and "openrouter.ai" in base_url else None
        )

    async def _record(self, task, model, resp):
        usage = getattr(resp, "usage", None)
        if self._usage_sink is None or usage is None:
            return
        try:
            await self._usage_sink(
                datetime.now(timezone.utc).isoformat(), task, model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                getattr(usage, "cost", None),
            )
        except Exception:
            log.exception("usage accounting failed")   # учёт не должен ронять основной запрос

    async def _parse(self, model, system, user, schema, task="other"):
        try:
            resp = await self._c.chat.completions.parse(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
                extra_body=self._route,
            )
        except Exception as e:
            if _is_outage(e):
                raise LLMUnavailable(str(e)) from e
            raise
        await self._record(task, model, resp)
        return resp.choices[0].message.parsed

    async def classify(self, text: str) -> str:
        out: Classification = await self._parse(self.fast, CLASSIFY_SYS, text, Classification, "classify")
        return out.kind

    async def parse_status(self, text: str, current_status: str | None) -> StatusUpdate:
        return await self._parse(
            self.fast, STATUS_SYS.format(current=current_status or "нет"), text, StatusUpdate,
            "status",
        )

    async def parse_mentor_verdict(self, text: str, current_status: str | None) -> StatusUpdate:
        return await self._parse(
            self.fast, VERDICT_SYS.format(current=current_status or "нет"), text, StatusUpdate,
            "verdict",
        )

    async def gen_ping(self, display, status, recent, profile, notes=None, avoid=None) -> str:
        stage = parse_stage(status)
        allowed, forbidden = ping_topics(stage)
        user = (
            f"Заметки ментора: {notes or 'нет'}\n"
            f"Досье: {profile or 'нет'}\n"
            f"Последняя переписка:\n{_dialog(recent) or 'нет'}\n"
            f"Ученик: {display}"
        )
        if avoid:
            user += (
                f"\n\nПрошлый вариант затронул запрещённое: {'; '.join(avoid)}. "
                f"Перепиши так, чтобы этого не было ни словом."
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
            "ping",
        )
        return out.text

    async def draft_answer(self, question, chunks, profile, examples=None, similar=None) -> str:
        ctx = "\n\n---\n\n".join(_chunk_text(c) for c in chunks) or "(материалы не найдены)"
        user = f"Вопрос ученика: {question}\n\nДосье: {profile or 'нет'}\n\nМатериалы курса:\n{ctx}"
        if similar:
            user += "\n\nПохожие вопросы, на которые ментор уже отвечал сам:\n" + "\n\n".join(
                f"Вопрос: {s['question'][:500]}\nОтвет ментора: {s['final'][:800]}" for s in similar
            )
        if examples:
            user += "\n\nКак ментор переписывал прошлые черновики:\n" + "\n\n".join(
                f"Черновик: {e['draft'][:500]}\nОтправил: {e['final'][:500]}" for e in examples
            )
        out: PlainText = await self._parse(self.smart, DRAFT_SYS, user, PlainText, "draft")
        return out.text

    async def update_profile(self, old, recent, notes=None) -> str:
        user = (
            f"Старое досье: {old or 'нет'}\n\n"
            f"Пометки ментора: {notes or 'нет'}\n\n"
            f"Свежая переписка:\n{_dialog(recent)}"
        )
        out: PlainText = await self._parse(self.fast, PROFILE_SYS, user, PlainText, "dossier")
        return out.text

    async def extract_interview(self, text: str) -> InterviewReport:
        return await self._parse(self.fast, INTERVIEW_SYS, text, InterviewReport, "interview")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._c.embeddings.create(model=self.embed_model, input=texts)
        except Exception as e:
            if _is_outage(e):
                raise LLMUnavailable(str(e)) from e
            raise
        await self._record("embed", self.embed_model, resp)
        return [d.embedding for d in resp.data]
