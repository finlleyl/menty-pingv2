import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher

from mentor_bot.cards import UNCERTAIN_LABEL
from mentor_bot.config import load_settings
from mentor_bot.jobs import (
    backup_db, digest_cycle, dossier_cycle, drain_pending, ping_cycle, remind_cycle,
)
from mentor_bot.health import due_catch_ups, tracked
from mentor_bot.kb import KBIndex, crawl, split_markdown
from mentor_bot.llm import LLM
from mentor_bot.routers import business, callbacks, commands
from mentor_bot.sender import Sender
from mentor_bot.service import Service
from mentor_bot.sheets import SheetsClient
from mentor_bot.store.repo import Repo

log = logging.getLogger("mentor_bot")


async def main():
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    repo = await Repo.open(settings.db_path)
    stuck = await repo.release_stuck()
    sheets = SheetsClient(settings.google_sa_path, settings.spreadsheet_id,
                          settings.active_sheet_titles, overrides=settings.header_overrides)
    llm = LLM(
        settings.llm_api_key, settings.llm_model_smart, settings.llm_model_fast, settings.embed_model,
        base_url=settings.llm_base_url, usage_sink=repo.log_usage,
    )
    kb = KBIndex(settings.kb_path)
    if not kb.load():
        log.warning("KB index empty — run /reindex")

    bot = Bot(token=settings.bot_token)
    sender = Sender(bot, repo, settings.mentor_user_id)
    service = Service(repo, sheets, llm, sender, kb, settings)
    if stuck:
        try:
            await report_stuck(stuck, sender)
        except Exception:
            log.exception("stuck sends report failed")
    try:
        await business.check_connection(bot, repo, sender, settings.mentor_user_id)
    except Exception:
        log.exception("business connection check failed on startup")
    try:
        await service.sync_mentees()
        log.info("loaded %d mentees from sheet", len(service.by_username))
    except Exception:
        log.exception("initial sheet sync failed")
        try:
            await sender.notify_mentor(
                "⚠️ Старт: не смог прочитать таблицу — проверь доступ сервисного аккаунта и ACTIVE_SHEETS"
            )
        except Exception:
            log.exception("mentor alert failed on startup")

    for title in settings.active_sheet_titles:
        try:
            if await sheets.ensure_dossier_column(title):
                log.info("created «Досье» column in sheet %s", title)
                await sender.notify_mentor(f"➕ В лист «{title}» добавлена колонка «Досье»")
        except Exception:
            log.exception("ensure_dossier_column failed for %s", title)

    async def reindex_fn():
        try:
            docs = await crawl(settings.edu_base_url, settings.edu_email, settings.edu_password)
            chunks: list[str] = []
            sources: list[str] = []
            for source, doc in docs:
                parts = split_markdown(doc)
                chunks.extend(parts)
                sources.extend([source] * len(parts))
            if not chunks:
                await sender.notify_mentor("⚠️ Reindex: контент не скачался (проверь EDU_EMAIL/EDU_PASSWORD)")
                return
            embeddings: list[list[float]] = []
            for i in range(0, len(chunks), 100):
                embeddings.extend(await llm.embed(chunks[i:i + 100]))
            kb.build(chunks, embeddings, sources)
            await sender.notify_mentor(f"✅ База знаний обновлена: {len(chunks)} фрагментов")
        except Exception as e:
            log.exception("reindex failed")
            await sender.notify_mentor(f"⚠️ Reindex упал: {e}")

    async def backup_fn():
        await backup_db(repo, sender)

    async def nightly_backup():
        await backup_fn()   # упадёт — предупредит обёртка tracked

    dp = Dispatcher()
    dp.include_router(commands.make_router(service, repo, sender, settings, reindex_fn, backup_fn))
    dp.include_router(callbacks.make_router(service, repo, sender))
    dp.include_router(business.make_router(service, repo, settings.mentor_user_id))

    from apscheduler.events import EVENT_JOB_MISSED
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    # misfire_grace_time по умолчанию — 1 секунда: занятый цикл событий (переиндексация,
    # долгий запрос) молча отменял запуск. Частым задачам — 5 минут, редким — 6 часов
    scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 300, "coalesce": True,
                                               "max_instances": 1})
    tz = ZoneInfo(settings.tz_name)
    rare = {"misfire_grace_time": 6 * 3600, "timezone": tz}
    jobs = {
        "ping_cycle": (ping_cycle, [service, repo, sender, llm, settings], {}),
        "remind_cycle": (remind_cycle, [repo, sender], {"settings": settings}),
        "drain_pending": (drain_pending, [service, repo, sender, settings], {}),
        "dossier_cycle": (dossier_cycle, [service, repo, llm, sender, settings], {}),
        "digest_cycle": (digest_cycle, [service, repo, sender, settings], {}),
        "nightly_backup": (nightly_backup, [], {}),
    }

    def add(name, trigger, **kw):
        fn, args, kwargs = jobs[name]
        scheduler.add_job(tracked(name, fn, repo, sender), trigger, id=name, args=args,
                          kwargs=kwargs, **kw)

    add("ping_cycle", "cron", minute=7)
    add("remind_cycle", "cron", minute="*/30")
    add("drain_pending", "cron", minute="*")
    add("dossier_cycle", "cron", hour=settings.dossier_hour, minute=13, **rare)
    add("digest_cycle", "cron", day_of_week=settings.digest_weekday,
        hour=settings.digest_hour, minute=3, **rare)
    if settings.backup_hour >= 0:
        add("nightly_backup", "cron", hour=settings.backup_hour, minute=41, **rare)

    def on_missed(event):
        # пропуск всё же случился (простой дольше допуска) — пусть будет виден в /health
        asyncio.get_running_loop().create_task(repo.job_missed(event.job_id))

    scheduler.add_listener(on_missed, EVENT_JOB_MISSED)
    scheduler.start()
    # бот лежал, когда должна была пройти редкая задача, — догоняем сразу после старта
    for name in due_catch_ups(await repo.job_runs(), datetime.now(timezone.utc)):
        if scheduler.get_job(name):
            log.info("catching up missed job %s", name)
            scheduler.get_job(name).modify(next_run_time=datetime.now(tz) + timedelta(minutes=1))

    log.info("starting polling")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        # без этого после падения polling оставался жив поток aiosqlite: процесс не выходил,
        # и Docker с restart: unless-stopped не перезапускал мёртвого бота
        scheduler.shutdown(wait=False)
        await finish_in_flight()
        await repo.close()
        await bot.session.close()


# docker stop ждёт 10 секунд до SIGKILL — успеваем дописать начатое
SHUTDOWN_GRACE = 8


async def finish_in_flight(timeout: float = SHUTDOWN_GRACE):
    """Дать доработать начатому: нажатию «Отправить», задаче пинга. Без этого asyncio.run
    отменял их на любом await — в том числе между отправкой ученику и записью «ушло»,
    и после рестарта бот не знал, отправлено ли сообщение."""
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if not others:
        return
    log.info("waiting for %d in-flight tasks", len(others))
    _, pending = await asyncio.wait(others, timeout=timeout)
    if pending:
        log.warning("%d tasks still running after %ss — cancelling", len(pending), timeout)


async def report_stuck(rows, sender):
    """Отправки, прерванные падением: карточки — итогом «проверь чат», ментору — список."""
    for r in rows:
        await sender.close_card(r.get("card_msg_id"), UNCERTAIN_LABEL, r["username"])
    users = ", ".join(sorted({f"@{r['username']}" for r in rows}))
    await sender.notify_mentor(
        f"⚠️ Бот перезапустился посреди отправки ({users}). Ушло ли сообщение — неизвестно: "
        f"проверь чат, повторно сам не отправляю"
    )


if __name__ == "__main__":
    asyncio.run(main())
