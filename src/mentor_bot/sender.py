import asyncio
import logging

from aiogram.types import BufferedInputFile

from mentor_bot.cards import done_kb

log = logging.getLogger(__name__)


class Sender:
    def __init__(self, bot, repo, mentor_user_id: int):
        self.bot = bot
        self.repo = repo
        self.mentor_user_id = mentor_user_id

    async def is_dryrun(self) -> bool:
        return await self.repo.get_setting("dryrun", "1") == "1"

    async def is_paused_all(self) -> bool:
        return await self.repo.get_setting("pause_all", "0") == "1"

    async def notify_mentor(self, text: str, reply_markup=None):
        return await self.bot.send_message(self.mentor_user_id, text, reply_markup=reply_markup)

    async def close_card(self, msg_id, label: str, username: str | None = None):
        """Заменить кнопки карточки на итог («✅ Отправлено 14:32») и «Открыть чат»."""
        if not msg_id:
            return   # карточка из старой версии или не дошла — менять нечего
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.mentor_user_id, message_id=msg_id,
                reply_markup=done_kb(label, username),
            )
        except Exception:
            # карточку удалили или она уже с этим итогом — не повод ронять само действие
            log.warning("card %s not updated", msg_id, exc_info=True)

    async def send_file_to_mentor(self, data: bytes, filename: str, caption: str = ""):
        await self.bot.send_document(
            self.mentor_user_id, BufferedInputFile(data, filename=filename), caption=caption or None
        )

    async def send_to_mentee(self, username: str, text: str) -> str:
        if await self.is_paused_all():
            return "paused"
        mentee = await self.repo.get_mentee(username)
        if await self.is_dryrun():
            await self.notify_mentor(f"[dry-run] → @{username}:\n{text}")
            return "dry"
        if not mentee or not mentee.get("chat_id"):
            return "no_chat"
        bconn = await self.repo.get_setting("bconn")
        if not bconn:
            return "no_bconn"
        await self.bot.send_message(
            mentee["chat_id"], text, business_connection_id=bconn
        )
        await asyncio.sleep(2)
        return "sent"
