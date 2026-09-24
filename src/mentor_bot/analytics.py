"""Аналитика воронки: сколько времени ученики проводят на стадиях, кто застрял, на чём
срезаются на собесах. Только чистые функции — данные приносит вызывающий код.

Про время на стадии. Наивное «среднее по тем, кто стадию уже прошёл» занижено: самые
медленные ещё в процессе и в выборку не попали (survivorship bias). Поэтому тех, кто
на стадии сейчас, считаем цензурированными справа — «пробыл уже t дней, и неизвестно,
сколько ещё» — и оцениваем функцию выживания S(t) = P(ещё на стадии через t дней)
оценкой Каплана–Мейера:

    S(t) = Π_{t_i ≤ t} (1 − d_i / n_i),

где t_i — моменты, когда кто-то ушёл со стадии, d_i — сколько ушло в t_i, n_i — сколько
было «под риском» (ещё на стадии и не цензурировано) прямо перед t_i. Медиана — первое t,
где S(t) ≤ 0.5. Если столько ещё никто не ушёл, медиана не определена — честное «мало данных».
"""
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from mentor_bot.pings import parse_iso_utc
from mentor_bot.stages import parse_stage

# Порядок конвейера — для вывода воронки
PIPELINE = ["sprint1", "sprint2", "sprint3", "sprint4", "resume", "legend", "mock",
            "market", "interviews", "offer", "paused", "unknown"]

# Стадии, у которых нет «нормального» срока: оффер — финал, пауза — не про скорость
UNTIMED = {"offer", "paused", "unknown"}


@dataclass
class Sample:
    days: float
    observed: bool   # True — ушёл со стадии; False — ещё на ней (цензурирован)


def stage_samples(history: list[dict], now_utc: datetime) -> dict[str, list[Sample]]:
    """status_history → длительности пребывания на стадиях.

    Отрезок учитывается, только если известен момент входа на стадию: первое наблюдение
    ('initial') говорит лишь, что ученик там уже был, а с какого дня — неизвестно.
    Переименования в пределах стадии («3 спринт» → «Спринт 3») переходом не считаются."""
    by_user: dict[str, list[dict]] = {}
    for h in history:
        by_user.setdefault(h["username"], []).append(h)

    out: dict[str, list[Sample]] = {}
    for events in by_user.values():
        events.sort(key=lambda h: (h["ts"], h["id"]))
        segments = []   # (стадия, момент входа, известен ли вход)
        for h in events:
            stage = parse_stage(h["to_status"])
            if segments and segments[-1][0] == stage:
                continue
            segments.append((stage, parse_iso_utc(h["ts"]), h["source"] != "initial"))
        for i, (stage, start, known) in enumerate(segments):
            if not known or stage in UNTIMED:
                continue
            if i + 1 < len(segments):
                end, observed = segments[i + 1][1], True
            else:
                end, observed = now_utc, False
            days = (end - start).total_seconds() / 86400
            out.setdefault(stage, []).append(Sample(max(days, 0.0), observed))
    return out


def kaplan_meier(samples: list[Sample]) -> list[tuple[float, float]]:
    """Ступенчатая оценка S(t): [(t_i, S(t_i))] в моменты ухода со стадии."""
    times = sorted({s.days for s in samples if s.observed})
    curve, surv = [], 1.0
    for t in times:
        at_risk = sum(1 for s in samples if s.days >= t)
        left = sum(1 for s in samples if s.observed and s.days == t)
        surv *= 1 - left / at_risk
        curve.append((t, surv))
    return curve


def km_quantile(samples: list[Sample], q: float) -> float | None:
    """Время, к которому стадию покидает доля q учеников (q=0.5 — медиана).
    None — столько ещё не ушло, оценка не определена."""
    for t, surv in kaplan_meier(samples):
        if surv <= 1 - q + 1e-12:
            return t
    return None


def naive_mean(samples: list[Sample]) -> float | None:
    """Среднее только по завершившим — для сравнения: именно так считать НЕ надо."""
    done = [s.days for s in samples if s.observed]
    return sum(done) / len(done) if done else None


def cluster(embs: list[list[float]], threshold: float) -> list[list[int]]:
    """Жадная кластеризация по косинусу: элемент идёт в кластер, к центроиду которого
    ближе threshold, иначе открывает новый. O(n·k) — для сотен вопросов хватает с запасом,
    и не нужно заранее знать число кластеров, как в k-means."""
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    for i, e in enumerate(embs):
        v = np.asarray(e, dtype=np.float64)
        v = v / (np.linalg.norm(v) or 1.0)
        best, best_sim = -1, threshold
        for ci, c in enumerate(centroids):
            sim = float(v @ c / (np.linalg.norm(c) or 1.0))
            if sim >= best_sim:
                best, best_sim = ci, sim
        if best < 0:
            clusters.append([i])
            centroids.append(v.copy())
        else:
            clusters[best].append(i)
            centroids[best] = centroids[best] + v   # сумма нормированных ∝ среднему направлению
    return clusters
