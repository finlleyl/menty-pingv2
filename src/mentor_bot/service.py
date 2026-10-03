import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from mentor_bot.llm import looks_like_interview, looks_like_verdict
from mentor_bot.pings import parse_iso_utc
from mentor_bot.stages import STAGE_LABELS, call_permission, mentee_may_propose, parse_stage
from mentor_bot.store.repo import KIND_HUMAN, KIND_QUESTION
from mentor_bot.style import TECH_KINDS, draft_problems, normalize_dashes, todo_marks

log = logging.getLogger(__name__)

# Порог косинусной близости, с которого прошлый вопрос считаем «тем же самым».
# Для text-embedding-3-small перефразировки одного вопроса обычно выше 0.7, разные темы — ниже.
SIMILAR_MIN = 0.7

# На что ментору готовим черновик. «спасибо/ок» и голая смена статуса — без черновика
REPLY_KINDS = ("tech_question", "org_question", "feelings", "win")

DIALOG_LIMIT = 10      # сколько прошлой переписки видят сортировщик и черновик
ONGOING_HOURS = 12     # писали друг другу в эти часы — диалог идёт, «Привет!» в ответе лишний
# У Telegram предел 4096 символов, но эмодзи считаются в UTF-16 за два — держим запас
NOTIFY_LIMIT = 4000


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def _prior_dialog(recent: list[dict], text: str) -> list[dict]:
    """Переписка до буфера. Новые сообщения уже лежат в messages хвостом — отрезаем их,
    иначе модель увидит их дважды и не отличит старое от нового."""
    out, rest = list(recent), text
    while rest and out and out[-1]["direction"] == "in":
        t = out[-1]["text"]
        if t == "[медиа]":
            out.pop()
        elif rest == t or rest.endswith("\n" + t):
            rest = rest[: -len(t)].removesuffix("\n")
            out.pop()
        else:
            break
    return out


_GREETING_RE = re.compile(r"\s*(?:привет|здравствуй|добр(?:ое|ый)|хай|салют|ку\b)", re.IGNORECASE)


def _ongoing(prior: list[dict], text: str, ts_iso: str) -> bool:
    """Диалог идёт — «Привет!» в ответе звучит как бот. Но если ученик поздоровался сам,
    ответить тем же нормально."""
    if not prior or _GREETING_RE.match(text):
        return False
    return parse_iso_utc(ts_iso) - parse_iso_utc(prior[-1]["ts"]) < timedelta(hours=ONGOING_HOURS)


def _kinds(tri) -> list[str]:
    """Метки сортировщика, поправленные кодом там, где ошибка дорога."""
    kinds = list(dict.fromkeys(tri.kinds)) or ["smalltalk"]
    # сдал спринт / легенда готова — успех: ровно здесь регламент разрешает позвать на собес
    # по спринту или мок, поэтому черновик нужен, даже если модель поставила одну смену этапа.
    # Статус по этим словам не предлагаем: этапы учёбы двигает только вердикт ментора
    if tri.milestone != "none" and not any(k in REPLY_KINDS for k in kinds):
        kinds.append("win")
    # человеку плохо — черновик нужен, даже если модель не поставила «переживания»
    if tri.urgent and not any(k in REPLY_KINDS for k in kinds):
        kinds.append("feelings")
    return kinds


def _header(username: str, kinds: list[str], urgent: bool) -> str:
    question = any(k in TECH_KINDS for k in kinds)
    if "feelings" in kinds:
        head = f"💬 @{username} делится и спрашивает:" if question else f"💬 @{username} делится:"
    elif "win" in kinds:
        head = f"🎉 @{username} делится успехом и спрашивает:" if question else f"🎉 @{username}:"
    else:
        head = f"❓ @{username} спрашивает:"
    return f"🔥 {head}" if urgent else head


@dataclass
class Draft:
    text: str
    problems: list[str]    # что осталось после переписывания — ментору строкой «⚠️ проверь»
    emb: list[float]
    kind: str              # группа для questions.kind
    similar: int


class Service:
    def __init__(self, repo, sheets, llm, sender, kb, settings):
        self.repo = repo
        self.sheets = sheets
        self.llm = llm
        self.sender = sender
        self.kb = kb
        self.settings = settings
        self.by_username: dict = {}

    async def sync_mentees(self):
        mentees = await self.sheets.load_mentees()
        self.by_username = {m.username: m for m in mentees}
        now_iso = datetime.now(timezone.utc).isoformat()
        # по by_username, а не по строкам: ник, записанный дважды, иначе «прыгал» бы
        # между двумя статусами на каждой синхронизации и засорял историю
        for m in self.by_username.values():
            await self.repo.upsert_mentee(m.username, sheet_title=m.sheet_title, row=m.row)
            # ручные правки статуса в таблице тоже попадают в историю и сдвигают status_since
            await self.repo.record_status(m.username, m.status, now_iso, "sheet")
        return self.by_username

    async def _propose(self, username: str, m, new_status: str, text: str, prefix: str,
                       hint: str = ""):
        if (m.status or "").strip() == new_status.strip():
            return  # статус уже такой — спрашивать нечего
        pid = await self.repo.add_proposal(username, new_status, m.status or "")
        await self.sender.notify_mentor(
            f"📋 {prefix}: {text[:200]}\n"
            f"Сменить статус «{m.status or '—'}» → «{new_status}»?{hint}",
            reply_markup=_kb([[("Да", f"st:yes:{pid}"), ("Нет", f"st:no:{pid}")]]),
        )

    async def _touch_sheet_date(self, username: str, ts_iso: str):
        m = self.by_username.get(username)
        if m is None:
            return
        msg_date = datetime.fromisoformat(ts_iso).astimezone(ZoneInfo(self.settings.tz_name)).date()
        if m.last_date is None or msg_date > m.last_date:
            await self.sheets.set_date(m, msg_date)
            m.last_date = msg_date

    async def on_contact_only(self, username: str, direction: str, ts_iso: str):
        """Медиа без текста: фиксируем контакт без классификации."""
        await self.repo.log_message(username, direction, "[медиа]", ts_iso)
        await self.repo.reset_unanswered(username)
        try:
            await self._touch_sheet_date(username, ts_iso)
        except Exception:
            log.exception("sheet date update failed")
        if direction == "in":
            # ученик ещё пишет — продлеваем окно дебаунса, если буфер уже открыт
            await self.repo.touch_pending(username, ts_iso)
        else:
            await self.repo.drop_pending(username)

    async def on_outgoing(self, username: str, text: str, ts_iso: str):
        await self.repo.log_message(username, "out", text, ts_iso)
        await self.repo.reset_unanswered(username)
        # ответ ментора своими словами — лучший пример для будущих черновиков
        await self.repo.record_manual_answer(username, text, datetime.fromisoformat(ts_iso))
        await self.repo.close_open_questions(username)
        # ментор ответил сам — накопленное обрабатывать не нужно, ждущий пинг тоже
        await self.repo.drop_pending(username)
        await self.repo.close_ping_drafts(username)
        try:
            await self._touch_sheet_date(username, ts_iso)
        except Exception:
            log.exception("sheet date update failed")
            await self.sender.notify_mentor(f"⚠️ Не смог обновить дату в таблице для @{username}")
        if looks_like_verdict(text):
            try:
                await self._propose_from_verdict(username, text)
            except Exception:
                log.exception("mentor verdict parsing failed")

    async def _propose_from_verdict(self, username: str, text: str):
        """Ментор написал «сдан спринт N» — предлагаем следующий статус кнопкой."""
        m = self.by_username.get(username)
        if m is None:
            return
        upd = await self.llm.parse_mentor_verdict(text, m.status)
        if not upd.new_status:
            return
        await self._propose(username, m, upd.new_status, text, f"Ты написал @{username}")

    async def on_incoming(self, username: str, text: str, ts_iso: str):
        await self.repo.log_message(username, "in", text, ts_iso)
        await self.repo.reset_unanswered(username)
        try:
            await self._touch_sheet_date(username, ts_iso)
        except Exception:
            log.exception("sheet date update failed")
            await self.sender.notify_mentor(f"⚠️ Не смог обновить дату в таблице для @{username}")
        # ученик вышел на связь — пинг, ждущий одобрения, больше не нужен
        await self.repo.close_ping_drafts(username)
        # LLM здесь НЕ дёргаем: копим в буфер, обработает drain_pending
        await self.repo.buffer_incoming(username, text, ts_iso)

    async def handle_buffered(self, username: str, text: str, ts_iso: str):
        """Разбор накопленного за окно дебаунса. Вызывается джобом drain_pending."""
        m = self.by_username.get(username)
        status = m.status if m else None
        recent = await self.repo.recent_messages(username, limit=DIALOG_LIMIT * 3)
        prior = _prior_dialog(recent, text)[-DIALOG_LIMIT:]
        tri = await self.llm.triage(text, prior, status)
        kinds = _kinds(tri)
        # все запросы к модели — до первого сообщения ментору: если провайдер упадёт посередине,
        # буфер повторится целиком, и черновик с предложением статуса не задвоятся
        upd = None
        if "status_change" in kinds and m is not None:
            upd = await self.llm.parse_status(text, status)
            # страховка к промпту: «закончил спринт» — повод для собеса, а не для кнопки статуса
            if upd.new_status and not mentee_may_propose(upd.new_status):
                log.info("status %r from %s's words skipped: learning stages move by verdict only",
                         upd.new_status, username)
                upd = None
        draft = None
        wants_reply = tri.needs_reply or tri.urgent or tri.milestone != "none"
        if wants_reply and any(k in REPLY_KINDS for k in kinds):
            draft = await self._compose_draft(username, m, text, ts_iso, tri, kinds, prior)
            # пока модель писала, ментор мог ответить в чате сам — карточка уже не нужна, а вопрос,
            # заведённый после его ответа, так и висел бы открытым
            last_out = await self.repo.last_out_ts(username)
            if last_out and parse_iso_utc(last_out) > parse_iso_utc(ts_iso):
                log.info("mentor replied to %s while drafting, draft dropped", username)
                draft = None
        try:
            if draft is not None:
                await self._deliver_draft(username, status, text, ts_iso, kinds, tri.urgent, draft)
        finally:
            # Telegram отверг черновик — предложение статуса («беру паузу») всё равно нужно:
            # сбой в соседнем шаге его не отменяет
            if upd is not None and upd.new_status:
                hint = "уверенно" if upd.confidence == "high" else "под вопросом"
                await self._propose(username, m, upd.new_status, text, f"@{username} написал",
                                    f" ({hint})")
        # фидбэк с собеса — последним и без права уронить разбор: основное уже сделано,
        # и повтор буфера из-за этого шага продублировал бы черновики и предложения
        if (m is not None and parse_stage(m.status) in ("interviews", "market")
                and looks_like_interview(text)):
            try:
                await self._collect_interview(username, text, ts_iso)
            except Exception:
                log.exception("interview extraction failed for %s", username)

    async def _compose_draft(self, username, m, text, ts_iso, tri, kinds, prior) -> Draft:
        """Черновик ответа: генерация → проверка кодом → при нарушении одна переписка."""
        status = m.status if m else None
        stage = parse_stage(status)
        # регламент считает код, а не модель: созвон только после спринта или к моку.
        # Человеку плохо — никаких приглашений: решать, что дальше, будет ментор
        call = None if tri.urgent else call_permission(tri.milestone, stage)
        # на стадии «Мок» разрешение висит на любом сообщении — без «легенда готова» модель
        # получает правило мягче, иначе пристегнёт «когда удобно мок?» и к «устал»
        if call == "mock" and tri.milestone != "legend_ready":
            call = "mock_stage"
        question = any(k in TECH_KINDS for k in kinds)
        group = KIND_QUESTION if question else KIND_HUMAN
        ongoing = _ongoing(prior, text, ts_iso)
        emb = (await self.llm.embed([text]))[0]
        samples = await self.repo.style_samples()
        similar = await self._similar_answers(emb)
        ctx = dict(
            recent=prior, stage_label=STAGE_LABELS[stage], status=status,
            notes=m.notes if m else None, profile=await self.repo.get_profile(username),
            # на «устал» или «сдал!» базу знаний не ищем вовсе: поиск всегда что-то вернёт,
            # и модель притянет к настроению пять случайных кусков про Go
            chunks=self.kb.search(text, emb, k=5) if question else None,
            similar=similar, examples=await self.repo.edit_examples(5, kind=group),
            samples=samples, call=call, urgent=tri.urgent, ongoing=ongoing,
        )
        reply = await self.llm.draft_reply(text, kinds, **ctx)
        problems = draft_problems(reply, kinds, call, ongoing, samples)
        if problems:
            log.warning("draft for %s failed lint %s, regenerating", username, problems)
            reply = await self.llm.draft_reply(text, kinds, **ctx, avoid=problems)
            problems = draft_problems(reply, kinds, call, ongoing, samples)
        return Draft(normalize_dashes(reply, samples), problems, emb, group, len(similar))

    async def _stage_line(self, username: str, status, ts_iso: str) -> str:
        """«📍 3-й спринт обучения · на стадии 12 дн.» — где ученик, чтобы не лезть в таблицу."""
        stage = parse_stage(status)
        if stage == "unknown":
            return ""
        line = f"\n📍 {STAGE_LABELS[stage]}"
        since = (await self.repo.get_mentee(username) or {}).get("status_since")
        if since:
            days = (parse_iso_utc(ts_iso) - parse_iso_utc(since)).days
            if days >= 0:
                line += f" · на стадии {days} дн."
        return line

    async def _deliver_draft(self, username, status, text, ts_iso, kinds, urgent, d: Draft):
        qid = await self.repo.add_question(username, text, d.text, ts_iso, emb=d.emb, kind=d.kind,
                                           emb_model=self._emb_model)
        # ментор читает каждый черновик, поэтому недочищенное не прячем, а подсвечиваем.
        # Пометка «допиши сам» — не ошибка модели, переписывать из-за неё нельзя (выдумает
        # факты), но и уйти ученику она не должна: «Отправить» с ней откажет
        warns = d.problems + [f"пометка {m} - замени через ✏️ Править" for m in todo_marks(d.text)]
        warn = f"\n\n⚠️ проверь: {'; '.join(warns)}" if warns else ""
        note = f"\n\n(учтено твоих прошлых ответов на похожее: {d.similar})" if d.similar else ""
        head = _header(username, kinds, urgent)
        tail = (f"{await self._stage_line(username, status, ts_iso)}"
                f"\n\nЧЕРНОВИК:\n{d.text}{warn}{note}")
        # ученик вставил лог на 3 тыс. символов — Telegram отверг бы всё сообщение. Полный
        # текст есть в чате и в questions, а черновик не режем: «Отправить» берёт его из базы
        room = NOTIFY_LIMIT - len(head) - len(tail) - 1
        shown = text if len(text) <= room else text[:max(200, room - 1)] + "…"
        try:
            await self.sender.notify_mentor(
                f"{head}\n{shown}{tail}",
                reply_markup=_kb([[("Отправить", f"q:send:{qid}"), ("✏️ Править", f"q:edit:{qid}"),
                                   ("Игнор", f"q:ign:{qid}")]]),
            )
        except Exception:
            # ментор черновик не увидел — открытым не держим, иначе remind_cycle напомнит
            # о сообщении, кнопок к которому у него нет
            await self.repo.set_question_state(qid, "ignored")
            raise

    async def _collect_interview(self, username: str, text: str, ts_iso: str):
        report = await self.llm.extract_interview(text)
        if report is None or not report.items:
            return
        embs = await self.llm.embed([it.question for it in report.items])
        await self.repo.add_interview_notes(username, ts_iso, report.items, embs,
                                            emb_model=self._emb_model)
        failed = [it.question for it in report.items if it.failed]
        lines = [f"🎯 @{username} про собес: записал вопросов — {len(report.items)}"]
        if failed:
            lines.append("Срезался на: " + "; ".join(q[:80] for q in failed[:5]))
        await self.sender.notify_mentor("\n".join(lines))

    @property
    def _emb_model(self):
        return getattr(self.llm, "embed_model", None)

    async def _similar_answers(self, emb, k: int = 3):
        """Прошлые вопросы, близкие по смыслу, вместе с тем, что ментор на них ответил."""
        rows = await self.repo.answered_questions(model=self._emb_model)
        # старые векторы без метки модели — только той же длины: другие не перемножить
        rows = [r for r in rows if len(r["emb"]) == len(emb)]
        if not rows:
            return []
        mat = np.array([r["emb"] for r in rows], dtype=np.float32)
        q = np.array(emb, dtype=np.float32)
        sims = mat @ q / (np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1e-9) + 1e-9)
        top = [i for i in np.argsort(-sims)[:k] if sims[i] >= SIMILAR_MIN]
        return [rows[i] for i in top]

    async def on_unknown_chat(self, username: str, display: str):
        titles = self.settings.active_sheet_titles
        buttons = [[(t, f"add:{i}:{username}")] for i, t in enumerate(titles)]
        buttons.append([("Не менти", f"add:skip:{username}")])
        await self.sender.notify_mentor(
            f"👤 Новый чат: {display} (@{username}). Добавить в таблицу?", reply_markup=_kb(buttons)
        )
