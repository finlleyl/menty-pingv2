from mentor_bot.stages import STAGE_LABELS, parse_stage


def test_parse_stage_sprints_both_word_orders():
    assert parse_stage("Спринт 1") == "sprint1"
    assert parse_stage("1 спринт") == "sprint1"
    assert parse_stage("Спринт 4") == "sprint4"
    assert parse_stage("4 спринт") == "sprint4"
    assert parse_stage("сПРИНТ 4") == "sprint4"      # регистр из реального листа
    assert parse_stage("Спринт2") == "sprint2"       # без пробела


def test_parse_stage_named_statuses():
    assert parse_stage("Собесы") == "interviews"
    assert parse_stage("Рынок") == "market"
    assert parse_stage("Поиск работы") == "market"
    assert parse_stage("приостановил") == "paused"
    assert parse_stage("занят") == "paused"
    assert parse_stage("оффер") == "offer"


def test_parse_stage_unknown():
    assert parse_stage(None) == "unknown"
    assert parse_stage("") == "unknown"
    assert parse_stage("   ") == "unknown"
    assert parse_stage("что-то своё") == "unknown"
    assert parse_stage("Спринт 7") == "unknown"      # спринтов только четыре


def test_every_stage_has_label():
    for stage in ("sprint1", "sprint2", "sprint3", "sprint4",
                  "interviews", "market", "paused", "offer", "unknown"):
        assert STAGE_LABELS[stage]
