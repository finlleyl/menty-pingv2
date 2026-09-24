import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from mentor_bot.pings import effective_last_contact, should_ping
from mentor_bot.routers.callbacks import EDIT_KEY, handle_edit_text

_bg_tasks: set = set()

HELP = (
    "/status — сводка\n/digest — недельная сводка по воронке\n/fails — на чём срезаются на собесах\n"
    "/pause @user N — пауза пингов\n/pause_all, /resume_all — стоп-кран\n"
    "/dryrun on|off — тестовый режим\n/pingmode auto|review — пинги сами или через тебя\n"
    "/cost [дней] — расходы на LLM\n/backup — бэкап базы файлом\n"
    "/cancel — отменить правку черновика\n/reindex — обновить базу знаний"
)


def _listing(usernames: list[str], limit: int = 30) -> str:
    if not usernames:
        return "—"
    shown = ", ".join("@" + u for u in usernames[:limit])
    rest = len(usernames) - limit
    return shown + (f" … и ещё {rest}" if rest > 0 else "")


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
        f"Пора пинговать ({len(due)}): " + _listing(due),
        f"Чат не привязан ({len(unbound)}): " + _listing(unbound),
        f"Открытых вопросов: {len(open_qs)}",
    ]
    return "\n".join(lines)


async def cost_text(args: str, repo, now_utc: datetime) -> str:
    days = int(args.strip()) if args.strip().isdigit() else 30
    rows = await repo.usage_summary((now_utc - timedelta(days=days)).isoformat())
    if not rows:
        return f"За {days} дн. запросов к модели не было"
    lines = [f"LLM за {days} дн.:"]
    total, unpriced = 0.0, 0
    for r in rows:
        cost = r["cost"]
        total += cost or 0.0
        unpriced += r["calls"] - r["priced"]
        money = f"${cost:.3f}" if cost is not None else "цена н/д"
        lines.append(
            f"• {r['task']}: {r['calls']} выз., {r['prompt_tokens']}→{r['completion_tokens']} ток., {money}"
        )
    lines.append(f"Итого: ${total:.2f}")
    if unpriced:
        # провайдер не вернул цену (не OpenRouter) — токены есть, денег нет
        lines.append(f"Без цены: {unpriced} выз.")
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


async def handle_pingmode(args: str, repo) -> str:
    val = args.strip().lower()
    if val not in ("auto", "review"):
        cur = await repo.get_setting("ping_mode", "auto")
        return f"Сейчас: {cur}. Формат: /pingmode auto|review"
    await repo.set_setting("ping_mode", val)
    if val == "review":
        return "Пинги теперь приходят тебе с кнопками «Отправить / Править / Пропустить»"
    return "Пинги уходят сами (кроме тех, что не прошли проверку тем)"


async def handle_pause_all(repo, on: bool) -> str:
    await repo.set_setting("pause_all", "1" if on else "0")
    return "Стоп-кран ВКЛ: ничего не шлю" if on else "Стоп-кран выключен"


def make_router(service, repo, sender, settings, reindex_fn, backup_fn=None) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private", F.from_user.id == settings.mentor_user_id)

    def args_of(message: Message) -> str:
        parts = (message.text or "").split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""

    @router.message(Command("status"))
    async def cmd_status(message: Message):
        try:
            await service.sync_mentees()  # /status всегда по свежему листу
        except Exception:
            await message.answer("⚠️ Не смог перечитать таблицу — показываю по кэшу")
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

    @router.message(Command("digest"))
    async def cmd_digest(message: Message):
        from mentor_bot.jobs import digest_cycle
        await digest_cycle(service, repo, sender, settings)

    @router.message(Command("fails"))
    async def cmd_fails(message: Message):
        from mentor_bot.digest import fails_text
        await message.answer(await fails_text(repo))

    @router.message(Command("pingmode"))
    async def cmd_pingmode(message: Message):
        await message.answer(await handle_pingmode(args_of(message), repo))

    @router.message(Command("reindex"))
    async def cmd_reindex(message: Message):
        task = asyncio.create_task(reindex_fn())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        await message.answer("Запустил переиндексацию базы знаний, отпишусь по готовности")

    @router.message(Command("cost"))
    async def cmd_cost(message: Message):
        await message.answer(await cost_text(args_of(message), repo, datetime.now(timezone.utc)))

    @router.message(Command("backup"))
    async def cmd_backup(message: Message):
        if backup_fn is None:
            await message.answer("Бэкап не настроен")
            return
        try:
            await backup_fn()
        except Exception as e:
            await message.answer(f"⚠️ Бэкап упал: {e}")

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message):
        await repo.set_setting(EDIT_KEY, "")
        await message.answer("Ок, правку отменил")

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message):
        await message.answer(HELP)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message):
        # обычный текст в личке бота — это правка черновика после кнопки «✏️ Править»
        reply = await handle_edit_text(message.text, repo, sender, service)
        await message.answer(reply or "Правку ничего не ждёт. /help — список команд")

    return router
