"""Тексты недельной сводки (/digest) и отчёта «на чём срезаются» (/fails)."""
from datetime import datetime, timedelta

from mentor_bot.analytics import PIPELINE, UNTIMED, cluster, km_quantile, stage_samples
from mentor_bot.pings import parse_iso_utc
from mentor_bot.stages import parse_stage

SHORT = {
    "sprint1": "Спринт 1", "sprint2": "Спринт 2", "sprint3": "Спринт 3", "sprint4": "Спринт 4",
    "resume": "Резюме", "legend": "Легенда", "mock": "Мок", "market": "Рынок",
    "interviews": "Собесы", "offer": "Оффер", "paused": "Пауза", "unknown": "?",
}

# Оценке «обычно проходят за N дней» верим, только если столько учеников реально ушли со стадии
MIN_EVENTS = 3

# Порог похожести вопросов с собесов (косинус эмбеддингов). Короткие перефразировки одного
# вопроса у text-embedding-3-small обычно выше 0.75; начни с него и подстрой по /fails.
FAIL_CLUSTER_MIN = 0.75


def _days(x: float) -> str:
    return f"{x:.0f} дн."


async def fails_text(repo, since_iso: str | None = None, top: int = 10) -> str:
    rows = await repo.interview_questions(failed_only=True, since_iso=since_iso)
    if not rows:
        return "Провалов на собесах пока не записано"
    groups = cluster([r["emb"] for r in rows], FAIL_CLUSTER_MIN)
    # сначала то, на чём срезались разные люди, а не один человек пять раз
    groups.sort(key=lambda g: (len({rows[i]["username"] for i in g}), len(g)), reverse=True)
    lines = [f"Срезались на собесах ({len(rows)} вопр., {len(groups)} тем):"]
    for g in groups[:top]:
        people = {rows[i]["username"] for i in g}
        companies = sorted({rows[i]["company"] for i in g if rows[i]["company"]})
        comp = f" [{', '.join(companies[:3])}]" if companies else ""
        lines.append(f"• {rows[g[0]]['question'][:120]} — {len(g)}× у {len(people)} чел.{comp}")
    return "\n".join(lines)


async def digest_text(service, repo, settings, now_utc: datetime) -> str:
    week_ago = now_utc - timedelta(days=7)
    history = await repo.status_history()
    samples = stage_samples(history, now_utc)

    # воронка по текущим статусам из таблицы
    counts: dict[str, int] = {}
    for m in service.by_username.values():
        st = parse_stage(m.status)
        counts[st] = counts.get(st, 0) + 1
    funnel = " · ".join(f"{SHORT[s]}: {counts[s]}" for s in PIPELINE if counts.get(s))
    lines = [f"📊 Сводка за неделю ({len(service.by_username)} менти)", f"Воронка: {funnel or '—'}"]

    # кто сдвинулся за неделю
    moved = []
    for h in history:
        if h["source"] == "initial" or parse_iso_utc(h["ts"]) < week_ago:
            continue
        if parse_stage(h["from_status"]) == parse_stage(h["to_status"]):
            continue
        moved.append(f"@{h['username']} {h['from_status'] or '—'} → {h['to_status']}")
    lines.append(f"\nСдвинулись ({len(moved)}): " + ("; ".join(moved) if moved else "никто"))

    # кто застрял: на стадии дольше, чем за это время её покидают 75% учеников
    stuck, silent = [], []
    for username, m in service.by_username.items():
        rec = await repo.get_mentee(username) or {}
        if rec.get("unanswered_pings", 0) >= settings.max_unanswered_pings:
            silent.append(f"@{username}")
        st = parse_stage(m.status)
        since = rec.get("status_since")
        if st in UNTIMED or not since:
            continue
        smp = samples.get(st, [])
        p75 = km_quantile(smp, 0.75) if sum(s.observed for s in smp) >= MIN_EVENTS else None
        elapsed = (now_utc - parse_iso_utc(since)).total_seconds() / 86400
        if p75 is not None and elapsed > p75:
            stuck.append((elapsed / p75, f"@{username} — {SHORT[st]} {_days(elapsed)} (75% проходят за {_days(p75)})"))
    stuck.sort(reverse=True)
    lines.append(f"Застряли ({len(stuck)}): " + ("; ".join(t for _, t in stuck) if stuck else "никто"))
    lines.append(f"Не отвечают на пинги ({len(silent)}): " + (", ".join(silent) if silent else "никто"))

    # сколько обычно занимает стадия — медиана Каплана–Мейера с учётом тех, кто ещё на ней
    timing = []
    for st in PIPELINE:
        smp = samples.get(st)
        if not smp:
            continue
        done = sum(s.observed for s in smp)
        med = km_quantile(smp, 0.5) if done >= MIN_EVENTS else None
        est = _days(med) if med is not None else "мало данных"
        timing.append(f"{SHORT[st]}: {est} (прошли {done}, сейчас на ней {len(smp) - done})")
    if timing:
        lines.append("\nМедиана времени на стадии:\n" + "\n".join(timing))

    fails = await repo.interview_questions(failed_only=True, since_iso=week_ago.isoformat())
    if fails:
        lines.append("\n" + await fails_text(repo, since_iso=week_ago.isoformat(), top=5))
    return "\n".join(lines)
