import pytest

from mentor_bot.style import ai_tells, call_hits, draft_problems, normalize_dashes


@pytest.mark.parametrize("text", [
    "Давай созвонимся завтра и разберём",
    "Можем созвон в четверг устроить",
    "позвоню тебе вечером",
    "Давай я перезвоню",
    "Кинь ссылку на зум",
    "го в zoom",
    "Скинь ссылку на google meet",
    "Давай в дискорде обсудим",
    "го в discord",
    "лучше голосом обсудим",
    "можно голосовой чат поднять",
    "давай по видеосвязи",
    "давай встретимся и разберём",
    "назначим встречу на пятницу",
    "при встрече покажу",
    "можем встречаться раз в неделю",
    "закинь call на завтра",
    "давай колл в 19",
    "Телемост подойдёт?",
    "Набери, обсудим по звонку",
    "Скину ссылку на телемосте",
    "Давай созвонимся с тобой в четверг",
])
def test_call_hits_catches_call_offers(text):
    assert call_hits(text), text


@pytest.mark.parametrize("text", [
    "Этот паттерн встречается в уроке про каналы",           # «встречается» = попадается
    "Я тоже встретил эту ошибку на первом спринте",
    "Спроси у коллеги, он такое делал",
    "Коллекция горутин растёт, глянь утечку",
    "Это всё зумеры придумали",
    "Посмотри call stack в панике",
    "Тут function call через интерфейс, поэтому аллокация",
    "Вызов идёт через `reflect.Value.Call(args)`",
    "```go\nresp := client.Call(ctx)\n```",
    "Как позвоночник после восьми часов за ноутом?",
    "Отправь код и текст ошибки, гляну",
    # собес ученика, а не созвон с ментором — на «Собесах» и «Рынке» об этом спрашивают законно
    "Как прошёл созвон с HR?",
    "Перед созвоном с тимлидом повтори каналы",
    "На встрече с работодателем спросят про опыт",
])
def test_call_hits_ignores_lookalikes(text):
    assert call_hits(text) == [], text


def test_ai_tells_finds_cliches_and_markdown():
    text = ("Отличный вопрос! Понимаю, как тебе непросто. **Главное** — не переживай.\n"
            "## Итог\nНадеюсь, это поможет, обращайся!")
    tells = ai_tells(text, ["feelings"])
    for label in ("«отличный вопрос»", "«понимаю, как тебе непросто»", "«не переживай»",
                  "markdown: **жирный**", "markdown: заголовки", "«надеюсь, это поможет»",
                  "«не стесняйся/обращайся»"):
        assert label in tells, label


def test_ai_tells_therapist_mirroring_and_triple_cheer():
    tells = ai_tells("Похоже, ты чувствуешь усталость. Это нормально чувствовать такое.", ["feelings"])
    assert "отзеркаливание чувств, как у психолога" in tells
    cheer = ai_tells("Молодец! Так держать! Всё получится!", ["win"])
    assert any("три восклицания" in t for t in cheer)
    assert not any("три восклицания" in t for t in ai_tells("Красава!!! Го дальше", ["win"]))


def test_bullets_flagged_only_outside_technical_replies():
    text = "Смотри:\n- закрывает отправитель\n- читать можно до конца"
    assert "markdown: список" in ai_tells(text, ["feelings"])
    assert "markdown: список" not in ai_tells(text, ["tech_question"])
    assert "markdown: список" not in ai_tells(text, ["feelings", "tech_question"])


def test_ai_tells_leaves_plain_human_text_alone():
    assert ai_tells("Да бывает, у меня на каналах тоже клинило. Скинь код, гляну", ["feelings"]) == []
    # «обращайся к полю» — техника, а не прощальная формула
    assert ai_tells("Обращайся к полю через указатель, иначе копия", ["tech_question"]) == []
    # «данные» в Go — это data, а не канцелярит
    assert ai_tells("Данные в канал пишет одна горутина", ["tech_question"]) == []


def test_greeting_flagged_only_in_ongoing_dialog():
    assert "«Привет!» посреди идущего диалога" in ai_tells("Привет! Скинь код", [], ongoing=True)
    assert ai_tells("Привет! Скинь код", [], ongoing=False) == []


def test_dashes_replaced_when_mentor_never_uses_them():
    samples = ["скинь код - гляну", "го дальше"]
    assert normalize_dashes("Канал — это труба — по ней данные", samples) == \
        "Канал - это труба - по ней данные"
    assert normalize_dashes("— ну что, как ты?", samples) == "- ну что, как ты?"
    assert normalize_dashes("диапазон 1–4 спринта", samples) == "диапазон 1–4 спринта"   # не тире


def test_dashes_kept_when_mentor_uses_them_or_unknown():
    assert normalize_dashes("Канал — труба", ["я тоже ставлю — тире"]) == "Канал — труба"
    assert normalize_dashes("Канал — труба", []) == "Канал — труба"   # образцов нет — не трогаем


def test_draft_problems_respects_call_permission():
    text = "Красава! Давай созвонимся на неделе и проведём собес по спринту"
    assert draft_problems(text, ["win"], call=None)[0].startswith("предлагает созвон")
    assert draft_problems(text, ["win"], call="sprint") == []
    assert draft_problems(text, ["win"], call="mock") == []
