import asyncio
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher

from mentor_bot.config import load_settings
from mentor_bot.jobs import (
    backup_db, digest_cycle, dossier_cycle, drain_pending, ping_cycle, remind_cycle,
)
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
    sheets = SheetsClient(settings.google_sa_path, settings.spreadsheet_id, settings.active_sheet_titles)
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
        try:
            await backup_fn()
        except Exception as e:
            log.exception("backup failed")
            await sender.notify_mentor(f"⚠️ Ночной бэкап упал: {e}")

    dp = Dispatcher()
    dp.include_router(commands.make_router(service, repo, sender, settings, reindex_fn, backup_fn))
    dp.include_router(callbacks.make_router(service, repo, sender))
    dp.include_router(business.make_router(service, repo, settings.mentor_user_id))

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(ping_cycle, "cron", minute=7,
                      args=[service, repo, sender, llm, settings],
                      max_instances=1, coalesce=True)
    scheduler.add_job(remind_cycle, "cron", minute="*/30", args=[repo, sender],
                      kwargs={"settings": settings}, max_instances=1)
    scheduler.add_job(drain_pending, "cron", minute="*",
                      args=[service, repo, sender, settings],
                      max_instances=1, coalesce=True)
    scheduler.add_job(dossier_cycle, "cron", hour=settings.dossier_hour, minute=13,
                      args=[service, repo, llm, sender, settings],
                      timezone=ZoneInfo(settings.tz_name),
                      max_instances=1, coalesce=True)
    scheduler.add_job(digest_cycle, "cron", day_of_week=settings.digest_weekday,
                      hour=settings.digest_hour, minute=3,
                      args=[service, repo, sender, settings],
                      timezone=ZoneInfo(settings.tz_name), max_instances=1, coalesce=True)
    if settings.backup_hour >= 0:
        scheduler.add_job(nightly_backup, "cron", hour=settings.backup_hour, minute=41,
                          timezone=ZoneInfo(settings.tz_name), max_instances=1, coalesce=True)
    scheduler.start()

    log.info("starting polling")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
