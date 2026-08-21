import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from mentor_bot.pings import effective_last_contact, should_ping

_bg_tasks: set = set()


async def status_text(service, repo, settings, now_utc: datetime) -> str:
    tz = ZoneInfo(settings.tz_name)
    dryrun = await repo.get_setting("dryrun", "1") == "1"
    pause_all = await repo.get_setting("pause_all", "0") == "1"
    due, unbound = [], []
    for username, m in service.by_username.items():
        rec = await repo.get_mentee(username) or {}
        if not rec.get("chat_id"):
            unbound.append(username)
        last = effective_last_contact(m.last_date, await repo.last_message_ts(username), tz)
        if should_ping(
            last_contact=last, status=m.status, now_utc=now_utc,
            stop_list=settings.stop_status_list, interval_days=settings.ping_interval_days,
            unanswered=rec.get("unanswered_pings", 0),
            max_unanswered=settings.max_unanswered_pings,
            paused_until_iso=rec.get("paused_until"),
        ):
            due.append(username)
    open_qs = await repo.open_questions()
    lines = [
        f"Менти в таблице: {len(service.by_username)}",
        f"dry-run: {'ON' if dryrun else 'OFF'} | pause_all: {'ON' if pause_all else 'OFF'}",
        f"Пора пинговать ({len(due)}): " + (", ".join("@" + u for u in due[:30]) or "—"),
        f"Чат не привязан ({len(unbound)}): " + (", ".join("@" + u for u in unbound[:30]) or "—"),
        f"Открытых вопросов: {len(open_qs)}",
    ]
    return "\n".join(lines)


async def handle_pause(args: str, repo) -> str:
    m = re.match(r"@?(\w+)\s+(\d+)", args.strip())
    if not m:
        return "Формат: /pause @username <дней>"
    username, days = m.group(1).lower(), int(m.group(2))
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    await repo.upsert_mentee(username)
    await repo.set_pause(username, until)
    return f"Пауза @{username} на {days} дн."


async def handle_dryrun(args: str, repo) -> str:
    val = args.strip().lower()
    if val not in ("on", "off"):
        return "Формат: /dryrun on|off"
    await repo.set_setting("dryrun", "1" if val == "on" else "0")
    return f"dry-run: {val.upper()}"


async def handle_pause_all(repo, on: bool) -> str:
    await repo.set_setting("pause_all", "1" if on else "0")
    return "Стоп-кран ВКЛ: ничего не шлю" if on else "Стоп-кран выключен"


def make_router(service, repo, sender, settings, reindex_fn) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private", F.from_user.id == settings.mentor_user_id)

    def args_of(message: Message) -> str:
        parts = (message.text or "").split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""

    @router.message(Command("status"))
    async def cmd_status(message: Message):
        await message.answer(await status_text(service, repo, settings, datetime.now(timezone.utc)))

    @router.message(Command("pause"))
    async def cmd_pause(message: Message):
        await message.answer(await handle_pause(args_of(message), repo))

    @router.message(Command("pause_all"))
    async def cmd_pause_all(message: Message):
        await message.answer(await handle_pause_all(repo, on=True))

    @router.message(Command("resume_all"))
    async def cmd_resume_all(message: Message):
        await message.answer(await handle_pause_all(repo, on=False))

    @router.message(Command("dryrun"))
    async def cmd_dryrun(message: Message):
        await message.answer(await handle_dryrun(args_of(message), repo))

    @router.message(Command("reindex"))
    async def cmd_reindex(message: Message):
        task = asyncio.create_task(reindex_fn())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        await message.answer("Запустил переиндексацию базы знаний, отпишусь по готовности")

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message):
        await message.answer(
            "/status — сводка\n/pause @user N — пауза пингов\n/pause_all, /resume_all — стоп-кран\n"
            "/dryrun on|off — тестовый режим\n/reindex — обновить базу знаний"
        )

    return router
