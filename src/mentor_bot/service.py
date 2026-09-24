import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from mentor_bot.llm import looks_like_verdict

log = logging.getLogger(__name__)

# Порог косинусной близости, с которого прошлый вопрос считаем «тем же самым».
# Для text-embedding-3-small перефразировки одного вопроса обычно выше 0.7, разные темы — ниже.
SIMILAR_MIN = 0.7


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


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
        for m in mentees:
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
        await self.repo.record_manual_answer(username, text)
        await self.repo.close_open_questions(username)
        # ментор ответил сам — накопленное обрабатывать не нужно
        await self.repo.drop_pending(username)
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
        # LLM здесь НЕ дёргаем: копим в буфер, обработает drain_pending
        await self.repo.buffer_incoming(username, text, ts_iso)

    async def handle_buffered(self, username: str, text: str, ts_iso: str):
        """Разбор накопленного за окно дебаунса. Вызывается джобом drain_pending."""
        kind = await self.llm.classify(text)
        m = self.by_username.get(username)
        if kind == "question":
            emb = (await self.llm.embed([text]))[0]
            chunks = self.kb.search(text, emb, k=5)
            profile = await self.repo.get_profile(username)
            similar = await self._similar_answers(emb)
            examples = await self.repo.edit_examples(5)
            draft = await self.llm.draft_answer(text, chunks, profile,
                                                examples=examples, similar=similar)
            qid = await self.repo.add_question(username, text, draft, ts_iso, emb=emb)
            note = f"\n\n(учтено твоих прошлых ответов на похожее: {len(similar)})" if similar else ""
            await self.sender.notify_mentor(
                f"❓ @{username} спрашивает:\n{text}\n\nЧЕРНОВИК:\n{draft}{note}",
                reply_markup=_kb([[("Отправить", f"q:send:{qid}"), ("✏️ Править", f"q:edit:{qid}"),
                                   ("Игнор", f"q:ign:{qid}")]]),
            )
        elif kind == "progress":
            upd = await self.llm.parse_status(text, m.status if m else None)
            if upd.new_status and m is not None:
                hint = "уверенно" if upd.confidence == "high" else "под вопросом"
                await self._propose(username, m, upd.new_status, text, f"@{username} написал",
                                    f" ({hint})")
    async def _similar_answers(self, emb, k: int = 3):
        """Прошлые вопросы, близкие по смыслу, вместе с тем, что ментор на них ответил."""
        rows = await self.repo.answered_questions()
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
