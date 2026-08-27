import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mentor_bot.pings import (
    _parse_iso_utc,
    effective_last_contact,
    in_send_window,
    should_ping,
)

log = logging.getLogger(__name__)


async def ping_cycle(service, repo, sender, llm, settings, now_utc: datetime | None = None):
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.tz_name)
    if await sender.is_paused_all():
        return
    if not in_send_window(now_utc.astimezone(tz), settings.quiet_start_hour, settings.quiet_end_hour):
        return
    try:
        await service.sync_mentees()
    except Exception:
        log.exception("sheet sync failed")
        await sender.notify_mentor("⚠️ Не смог прочитать таблицу, цикл пингов пропущен")
        return

    errors = 0
    items = list(service.by_username.items())
    random.shuffle(items)
    for username, m in items:
        rec = await repo.get_mentee(username) or {}
        last_msg_iso = await repo.last_message_ts(username)
        last = effective_last_contact(m.last_date, last_msg_iso, tz)
        # рассинхрон: в чате связь свежее, чем дата в таблице → таблица догоняет телеграм
        if last_msg_iso:
            msg_date = datetime.fromisoformat(last_msg_iso).astimezone(tz).date()
            if m.last_date is None or msg_date > m.last_date:
                try:
                    await service.sheets.set_date(m, msg_date)
                    m.last_date = msg_date
                except Exception:
                    log.exception("sheet date backfill failed for %s", username)
                    errors += 1
        if not should_ping(
            last_contact=last, status=m.status, now_utc=now_utc,
            stop_list=settings.stop_status_list, interval_days=settings.ping_interval_days,
            unanswered=rec.get("unanswered_pings", 0),
            max_unanswered=settings.max_unanswered_pings,
            paused_until_iso=rec.get("paused_until"),
        ):
            continue

        last_ping = await repo.last_ping_ts(username)
        if last_ping is not None:
            last_ping_local = datetime.fromisoformat(last_ping).astimezone(tz).date()
            if last_ping_local == now_utc.astimezone(tz).date():
                continue  # уже пинговали сегодня — не чаще 1 пинга в сутки

        dry = await sender.is_dryrun()
        if not dry:
            if not rec.get("chat_id"):
                continue  # чат не привязан — не тратим LLM зря
            if not await repo.get_setting("bconn"):
                if not await repo.get_setting("alerted_no_bconn"):
                    await sender.notify_mentor(
                        "⚠️ Business connection не подключён — пинги не идут"
                    )
                    await repo.set_setting("alerted_no_bconn", "1")
                break  # без подключения дальше по циклу смысла нет

        try:
            recent = await repo.recent_messages(username, limit=10)
            profile = await repo.get_profile(username)
            text = await llm.gen_ping(m.display, m.status, recent, profile, m.notes)
        except Exception:
            log.exception("ping generation failed for %s", username)
            errors += 1
            continue

        await repo.log_ping(username, now_utc.isoformat(), "attempt")
        try:
            result = await sender.send_to_mentee(username, text)
        except Exception:
            log.exception("ping send failed for %s", username)
            errors += 1
            continue

        if result == "sent":
            await repo.log_ping(username, now_utc.isoformat(), "sent")
            await repo.bump_unanswered(username)
            await repo.set_setting("alerted_no_bconn", "")
            rec2 = await repo.get_mentee(username) or {}
            if rec2.get("unanswered_pings", 0) >= settings.max_unanswered_pings:
                await sender.notify_mentor(
                    f"🚨 @{username} игнорит {settings.max_unanswered_pings} пинга подряд — "
                    f"дальше не пингую, разберись вручную"
                )
            try:
                await service.sheets.set_date(m, now_utc.astimezone(tz).date())
                m.last_date = now_utc.astimezone(tz).date()
            except Exception:
                log.exception("sheet date write failed after ping")
                await sender.notify_mentor(f"⚠️ Пинг @{username} ушёл, но дата в таблице не записана")
            await repo.log_message(username, "out", text, now_utc.isoformat())
            await asyncio.sleep(58)  # rate limit ≤1 пинг/мин
        elif result == "dry":
            await repo.log_ping(username, now_utc.isoformat(), "dry")

    if errors:
        await sender.notify_mentor(f"⚠️ Цикл пингов: {errors} ошибок, детали в логах")



async def drain_pending(service, repo, sender, settings, now_utc: datetime | None = None):
    """Разбирает буферы, в которые ученик не писал дольше окна дебаунса."""
    now_utc = now_utc or datetime.now(timezone.utc)
    before = (now_utc - timedelta(minutes=settings.debounce_minutes)).isoformat()
    for row in await repo.mature_pending(before):
        username = row["username"]
        last_out = await repo.last_out_ts(username)
        if last_out and _parse_iso_utc(last_out) > _parse_iso_utc(row["last_in_ts"]):
            # ментор ответил сам, пока буфер зрел — LLM не трогаем
            await repo.drop_pending(username)
            continue
        text = "\n".join(json.loads(row["texts"]))
        try:
            await service.handle_buffered(username, text, row["last_in_ts"])
        except Exception:
            log.exception("drain failed for %s", username)
            await sender.notify_mentor(f"⚠️ Ошибка обработки сообщений @{username}: {text[:100]}")
        finally:
            await repo.drop_pending(username)

async def remind_cycle(repo, sender, now_utc: datetime | None = None, settings=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    if settings is not None:
        tz = ZoneInfo(settings.tz_name)
        hour = now_utc.astimezone(tz).hour
        if not (9 <= hour < 23):
            return
    threshold = (now_utc - timedelta(hours=4)).isoformat()
    for q in await repo.open_questions(older_than_iso=threshold, unreminded_only=True):
        await sender.notify_mentor(
            f"⏰ Висит вопрос от @{q['username']} ({q['created_ts'][:16]}):\n{q['question'][:200]}"
        )
        await repo.mark_reminded(q["id"])


async def dossier_cycle(service, repo, llm, sender, settings, now_utc: datetime | None = None):
    """Раз в сутки обновляет досье тех, у кого с прошлого раза была переписка."""
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        await service.sync_mentees()
    except Exception:
        log.exception("sheet sync failed")
        await sender.notify_mentor("⚠️ Досье: не смог прочитать таблицу, цикл пропущен")
        return

    errors = 0
    for username in await repo.stale_profiles():
        m = service.by_username.get(username)
        if m is None:
            continue  # чат есть, а в таблице человека нет — не наш менти
        try:
            recent = await repo.recent_messages(username, limit=30)
            old = await repo.get_profile(username)
            summary = await llm.update_profile(old, recent, m.notes)
            await repo.set_profile(username, summary, now_utc.isoformat())
            await service.sheets.set_dossier(m, summary)
        except Exception:
            log.exception("dossier update failed for %s", username)
            errors += 1
    if errors:
        await sender.notify_mentor(f"⚠️ Досье: {errors} ошибок, детали в логах")
