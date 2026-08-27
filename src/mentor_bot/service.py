import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)


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
        for m in mentees:
            await self.repo.upsert_mentee(m.username, sheet_title=m.sheet_title, row=m.row)
        return self.by_username

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

    async def on_outgoing(self, username: str, text: str, ts_iso: str):
        await self.repo.log_message(username, "out", text, ts_iso)
        await self.repo.reset_unanswered(username)
        await self.repo.close_open_questions(username)
        try:
            await self._touch_sheet_date(username, ts_iso)
        except Exception:
            log.exception("sheet date update failed")
            await self.sender.notify_mentor(f"⚠️ Не смог обновить дату в таблице для @{username}")

    async def on_incoming(self, username: str, text: str, ts_iso: str):
        await self.repo.log_message(username, "in", text, ts_iso)
        await self.repo.reset_unanswered(username)
        try:
            await self._touch_sheet_date(username, ts_iso)
        except Exception:
            log.exception("sheet date update failed")
            await self.sender.notify_mentor(f"⚠️ Не смог обновить дату в таблице для @{username}")
        try:
            await self._handle_content(username, text, ts_iso)
        except Exception:
            log.exception("llm handling failed")
            await self.sender.notify_mentor(f"⚠️ Ошибка обработки сообщения @{username}: {text[:100]}")

    async def _handle_content(self, username: str, text: str, ts_iso: str):
        kind = await self.llm.classify(text)
        m = self.by_username.get(username)
        if kind == "question":
            emb = (await self.llm.embed([text]))[0]
            chunks = self.kb.search(text, emb, k=5)
            profile = await self.repo.get_profile(username)
            draft = await self.llm.draft_answer(text, chunks, profile)
            qid = await self.repo.add_question(username, text, draft, ts_iso)
            await self.sender.notify_mentor(
                f"❓ @{username} спрашивает:\n{text}\n\nЧЕРНОВИК:\n{draft}",
                reply_markup=_kb([[("Отправить", f"q:send:{qid}"), ("Игнор", f"q:ign:{qid}")]]),
            )
        elif kind == "progress":
            upd = await self.llm.parse_status(text, m.status if m else None)
            if upd.new_status and m is not None:
                pid = await self.repo.add_proposal(username, upd.new_status)
                hint = "уверенно" if upd.confidence == "high" else "под вопросом"
                await self.sender.notify_mentor(
                    f"📋 @{username} написал: {text[:200]}\n"
                    f"Сменить статус на «{upd.new_status}»? ({hint})",
                    reply_markup=_kb([[("Да", f"st:yes:{pid}"), ("Нет", f"st:no:{pid}")]]),
                )
        # обновить досье после любой содержательной реплики
        if kind in ("question", "progress"):
            recent = await self.repo.recent_messages(username, limit=15)
            old = await self.repo.get_profile(username)
            summary = await self.llm.update_profile(old, recent)
            await self.repo.set_profile(username, summary, ts_iso)

    async def on_unknown_chat(self, username: str, display: str):
        titles = self.settings.active_sheet_titles
        buttons = [[(t, f"add:{i}:{username}")] for i, t in enumerate(titles)]
        buttons.append([("Не менти", f"add:skip:{username}")])
        await self.sender.notify_mentor(
            f"👤 Новый чат: {display} (@{username}). Добавить в таблицу?", reply_markup=_kb(buttons)
        )
