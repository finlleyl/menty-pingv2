from datetime import datetime, timezone

import pytest

from mentor_bot.analytics import (
    Sample, cluster, kaplan_meier, km_quantile, naive_mean, stage_samples,
)

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_kaplan_meier_by_hand():
    # ушли на 5, 8, 12 днях; 10 и 15 — ещё на стадии (цензурированы)
    s = [Sample(5, True), Sample(8, True), Sample(10, False), Sample(12, True), Sample(15, False)]
    # t=5: под риском 5 → S=4/5; t=8: под риском 4 → ×3/4; t=12: под риском 2 (12 и 15) → ×1/2
    curve = kaplan_meier(s)
    assert [t for t, _ in curve] == [5, 8, 12]
    assert [v for _, v in curve] == pytest.approx([0.8, 0.6, 0.3])
    assert km_quantile(s, 0.5) == 12
    assert km_quantile(s, 0.75) is None          # до S ≤ 0.25 данные не дотягивают
    # наивное среднее по завершившим занижает срок: медленные ещё не закончили
    assert naive_mean(s) == pytest.approx(25 / 3)
    assert naive_mean(s) < km_quantile(s, 0.5)


def h(user, frm, to, ts, source="sheet", i=0):
    return {"id": i, "username": user, "from_status": frm, "to_status": to, "ts": ts, "source": source}


def test_stage_samples_skip_unknown_entry_and_renames():
    history = [
        h("a", None, "Спринт 1", "2026-08-01T00:00:00+00:00", "initial"),  # вход неизвестен
        h("a", "Спринт 1", "Спринт 2", "2026-08-05T00:00:00+00:00"),
        h("a", "Спринт 2", "2 спринт", "2026-08-06T00:00:00+00:00"),       # переименование
        h("a", "2 спринт", "Спринт 3", "2026-08-15T00:00:00+00:00", "bot"),
        h("b", None, "Спринт 2", "2026-08-20T00:00:00+00:00", "bot"),
    ]
    got = stage_samples(history, NOW)
    assert "sprint1" not in got
    assert sorted((s.days, s.observed) for s in got["sprint2"]) == [(10.0, True), (12.0, False)]
    assert [(s.days, s.observed) for s in got["sprint3"]] == [(17.0, False)]


def test_cluster_groups_paraphrases():
    embs = [[1.0, 0.0], [0.98, 0.2], [0.0, 1.0], [0.1, 0.99]]
    assert cluster(embs, 0.9) == [[0, 1], [2, 3]]
    assert cluster(embs, 0.999) == [[0], [1], [2], [3]]


def test_pause_or_rollback_is_censored_not_completed():
    history = [
        h("a", None, "Спринт 2", "2026-08-01T00:00:00+00:00", "bot"),
        h("a", "Спринт 2", "приостановил", "2026-08-05T00:00:00+00:00"),
        h("b", None, "Спринт 3", "2026-08-01T00:00:00+00:00", "bot"),
        h("b", "Спринт 3", "Спринт 2", "2026-08-02T00:00:00+00:00"),       # откат-исправление
    ]
    got = stage_samples(history, NOW)
    assert [(s.days, s.observed) for s in got["sprint2"]] == [(4.0, False), (30.0, False)]
    assert [(s.days, s.observed) for s in got["sprint3"]] == [(1.0, False)]
