import asyncio


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
        await self.bot.send_message(self.mentor_user_id, text, reply_markup=reply_markup)

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
