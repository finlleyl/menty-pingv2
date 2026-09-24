import json

from mentor_bot.kb import KBIndex, html_to_chunks, recall_at_k, tokenize

HTML = """
<html><body><nav>меню</nav>
<h1>Горутины</h1><p>Горутина — легковесный поток. Запуск: go f().</p>
<h1>Каналы</h1><p>Канал — способ связи горутин. make(chan int).</p>
</body></html>
"""


def test_html_to_chunks():
    chunks = html_to_chunks(HTML)
    assert len(chunks) >= 2
    assert any("Горутина" in c for c in chunks)
    assert any("Канал" in c for c in chunks)


def fake_emb(text: str) -> list[float]:
    # игрушечный эмбеддинг: частоты букв
    return [float(text.lower().count(ch)) for ch in "абвгк каналгорутин"]


def test_index_search(tmp_path):
    chunks = ["Горутина — легковесный поток", "Канал — связь горутин", "Слайсы и мапы"]
    idx = KBIndex(str(tmp_path))
    idx.build(chunks, [fake_emb(c) for c in chunks])

    idx2 = KBIndex(str(tmp_path))
    assert idx2.load()
    got = idx2.search("что такое канал", fake_emb("что такое канал"), k=2)
    assert "Канал — связь горутин" in [h["text"] for h in got]


def test_index_load_missing(tmp_path):
    assert KBIndex(str(tmp_path / "nope")).load() is False


def test_empty_index_build_and_load(tmp_path):
    idx = KBIndex(str(tmp_path))
    idx.build([], [])                     # не должно падать
    assert idx.search("что-то", [0.0]) == []
    idx2 = KBIndex(str(tmp_path))
    assert idx2.load() is True
    assert idx2.search("что-то", [0.0]) == []


def test_long_paragraph_splits_on_word_boundary():
    long_html = "<html><body><h1>Тема</h1><p>" + ("слово " * 600) + "</p></body></html>"
    chunks = html_to_chunks(long_html)
    assert len(chunks) >= 2
    for c in chunks:
        assert not c.endswith("слов")      # нет разреза посреди слова
        assert "словослово" not in c


def test_tokenize_stems_russian_and_keeps_go_identifiers():
    assert tokenize("Горутины, горутина!") == ["горутин", "горутин"]
    assert tokenize("sync.WaitGroup") == ["sync", "waitgroup"]


def test_bm25_matches_other_word_form():
    chunks = ["Слайсы и мапы", "Горутина — легковесный поток", "Интерфейсы"]
    idx = KBIndex("/unused")
    idx.chunks = chunks
    idx._fit_bm25()
    scores = idx._bm25.get_scores(tokenize("что такое горутины?"))
    # «горутины» в вопросе и «Горутина» в тексте совпали только благодаря стеммингу
    assert max(range(3), key=lambda i: scores[i]) == 1 and scores[1] > 0


def test_rrf_prefers_consensus_over_single_leader():
    from mentor_bot.kb import rrf_scores
    n = 500
    # 0 — первый по косинусу и последний по BM25, n-1 — наоборот; 1 — второй в обоих
    cos_order = list(range(n))
    bm_order = [n - 1, 1] + list(range(2, n - 1)) + [0]
    assert rrf_scores((cos_order, bm_order), n).argmax() == 1
    # при k=0 одиночный лидер перебивает консенсус — поэтому k и нужен
    assert rrf_scores((cos_order, bm_order), n, k=0)[0] > rrf_scores((cos_order, bm_order), n, k=0)[1]


def test_sources_roundtrip_and_legacy_format(tmp_path):
    idx = KBIndex(str(tmp_path))
    idx.build(["про каналы"], [[1.0]], ["Спринт «2» → урок «Каналы»"])
    idx2 = KBIndex(str(tmp_path))
    idx2.load()
    assert idx2.search("каналы", [1.0])[0] == {"text": "про каналы", "source": "Спринт «2» → урок «Каналы»"}
    # индекс, собранный прошлой версией: список строк
    (tmp_path / "chunks.json").write_text(json.dumps(["старый фрагмент"]))
    idx3 = KBIndex(str(tmp_path))
    idx3.load()
    assert idx3.search("фрагмент", [1.0])[0] == {"text": "старый фрагмент", "source": ""}


def test_recall_at_k():
    class StubIndex:
        def search(self, q, emb, k=5):
            if "канал" in q:
                return [{"text": "Канал — связь горутин", "source": "урок «Каналы»"}]
            return [{"text": "Горутина — поток", "source": "урок «Горутины»"}]

    cases = [{"question": "что такое канал", "expected": "Каналы"},
             {"question": "что такое мапа", "expected": "Мапы"}]
    recall, misses = recall_at_k(StubIndex(), cases, [[0.0], [0.0]], k=1)
    assert recall == 0.5 and misses == ["что такое мапа"]
