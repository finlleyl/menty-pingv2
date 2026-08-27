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


def test_sprint4_is_still_learning():
    # 4-й спринт — учёба: резюме, легенда и HR туда не лезут
    allowed, forbidden = ping_topics("sprint4")
    joined_allowed = " ".join(allowed).lower()
    joined_forbidden = " ".join(forbidden).lower()
    assert "резюме" not in joined_allowed and "hr" not in joined_allowed
    assert "резюме" in joined_forbidden
    assert "легенд" in joined_forbidden
    assert "hr" in joined_forbidden
    assert "оффер" in joined_forbidden
    assert ping_topics("sprint4") == ping_topics("sprint1")   # спринт есть спринт


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


def test_parse_stage_pipeline_after_sprints():
    assert parse_stage("Резюме") == "resume"
    assert parse_stage("резюме") == "resume"
    assert parse_stage("Легенда") == "legend"
    assert parse_stage("легенду пишет") == "legend"
    assert parse_stage("Мок") == "mock"
    assert parse_stage("мок-собес") == "mock"      # «мок» важнее «собес»


def test_resume_stage_waits_on_mentor():
    allowed, forbidden = ping_topics("resume")
    joined_allowed = " ".join(allowed).lower()
    joined_forbidden = " ".join(forbidden).lower()
    assert "резюме" in joined_allowed              # можно сказать, что резюме готовится
    assert "спринт" in joined_forbidden and "собес" in joined_forbidden
    assert "легенд" in joined_forbidden            # легенда — следующий шаг, не этот


def test_legend_stage_asks_about_legend():
    allowed, forbidden = ping_topics("legend")
    assert any("легенд" in a.lower() for a in allowed)
    joined_forbidden = " ".join(forbidden).lower()
    assert "собес" in joined_forbidden and "рынок" in joined_forbidden
    assert "спринт" in joined_forbidden


def test_mock_stage_asks_about_the_mock():
    allowed, forbidden = ping_topics("mock")
    assert any("мок" in a.lower() for a in allowed)
    joined_forbidden = " ".join(forbidden).lower()
    assert "рынок" in joined_forbidden and "оффер" in joined_forbidden


def test_new_stages_have_labels():
    for stage in ("resume", "legend", "mock"):
        assert STAGE_LABELS[stage]
