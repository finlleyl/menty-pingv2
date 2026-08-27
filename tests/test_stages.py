from mentor_bot.stages import STAGE_LABELS, parse_stage, ping_topics


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


def test_sprint_stages_forbid_market_talk():
    for stage in ("sprint1", "sprint2", "sprint3"):
        allowed, forbidden = ping_topics(stage)
        joined_forbidden = " ".join(forbidden).lower()
        assert "собес" in joined_forbidden
        assert "рынок" in joined_forbidden
        assert any("спринт" in a.lower() for a in allowed)


def test_sprint4_allows_resume_and_hr():
    allowed, forbidden = ping_topics("sprint4")
    joined_allowed = " ".join(allowed).lower()
    assert "резюме" in joined_allowed and "hr" in joined_allowed
    assert any("оффер" in f.lower() for f in forbidden)


def test_market_stage_forbids_sprint_talk():
    allowed, forbidden = ping_topics("market")
    assert any("спринт" in f.lower() for f in forbidden)
    assert any("отклик" in a.lower() for a in allowed)


def test_interviews_stage():
    allowed, forbidden = ping_topics("interviews")
    assert any("собес" in a.lower() for a in allowed)
    assert any("спринт" in f.lower() for f in forbidden)


def test_unknown_stage_is_neutral_and_safe():
    allowed, forbidden = ping_topics("unknown")
    assert allowed and forbidden
    assert any("собес" in f.lower() for f in forbidden)


def test_ping_topics_never_raises_on_any_stage():
    for stage in STAGE_LABELS:
        allowed, forbidden = ping_topics(stage)
        assert isinstance(allowed, list) and isinstance(forbidden, list)
