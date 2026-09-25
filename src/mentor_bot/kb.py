import json
import os
import re
import numpy as np

# Константа RRF: 1/(k + rank). При k≈60 выигрывает документ, который высоко в ОБОИХ
# списках, а не одиночный лидер одного из них (при k=0 первое место весит как два вторых).
RRF_K = 60

_WORD_RE = re.compile(r"\w+")
_CYR_RE = re.compile(r"[а-яё]")
_stemmer = None


def tokenize(text: str) -> list[str]:
    """Токены для BM25: без пунктуации, русские слова — к основе («горутины» → «горутин»).
    Латиницу не трогаем: это идентификаторы Go (WaitGroup, ctx), их стемминг только портит."""
    global _stemmer
    if _stemmer is None:
        import snowballstemmer
        _stemmer = snowballstemmer.stemmer("russian")
    out = []
    for w in _WORD_RE.findall(text.lower()):
        out.append(_stemmer.stemWord(w) if _CYR_RE.search(w) else w)
    return out


def split_markdown(md: str, max_chars: int = 1500) -> list[str]:
    """Markdown → чанки: по заголовкам, крупные секции — по абзацам/словам."""
    sections = re.split(r"(?m)^(?=#)", md)
    chunks: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if len(sec) < 40:
            continue
        while len(sec) > max_chars:
            cut = sec.rfind("\n\n", 0, max_chars)
            if cut <= 200:
                cut = sec.rfind("\n", 0, max_chars)
            if cut <= 200:
                cut = sec.rfind(" ", 0, max_chars)
            if cut <= 200:
                cut = max_chars
            chunks.append(sec[:cut].strip())
            sec = sec[cut:].strip()
        if len(sec) >= 40:
            chunks.append(sec)
    return chunks


def html_to_chunks(html: str, max_chars: int = 1500) -> list[str]:
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["nav", "script", "style", "header", "footer"]):
        tag.decompose()
    md = markdownify(str(soup), heading_style="ATX")
    return split_markdown(md, max_chars)


def rrf_scores(orders, n: int, k: int = RRF_K) -> np.ndarray:
    """Reciprocal rank fusion: у каждого документа сумма 1/(k + ранг) по всем спискам."""
    score = np.zeros(n)
    for order in orders:
        for rank, i in enumerate(order, start=1):
            score[i] += 1.0 / (k + rank)
    return score


class KBIndex:
    def __init__(self, path: str):
        self.path = path
        self.chunks: list[str] = []
        self.sources: list[str] = []
        self._emb: np.ndarray | None = None
        self._bm25 = None

    def _fit_bm25(self):
        if not self.chunks:
            self._bm25 = None
            return
        from rank_bm25 import BM25Okapi
        self._bm25 = BM25Okapi([tokenize(c) or ["_"] for c in self.chunks])

    def build(self, chunks: list[str], embeddings: list[list[float]], sources: list[str] | None = None):
        os.makedirs(self.path, exist_ok=True)
        self.chunks = chunks
        self.sources = sources or [""] * len(chunks)
        self._emb = np.array(embeddings, dtype=np.float32)
        with open(os.path.join(self.path, "chunks.json"), "w") as f:
            json.dump([{"text": c, "source": src} for c, src in zip(chunks, self.sources)],
                      f, ensure_ascii=False)
        np.save(os.path.join(self.path, "emb.npy"), self._emb)
        self._fit_bm25()

    def load(self) -> bool:
        cpath = os.path.join(self.path, "chunks.json")
        epath = os.path.join(self.path, "emb.npy")
        if not (os.path.exists(cpath) and os.path.exists(epath)):
            return False
        with open(cpath) as f:
            raw = json.load(f)
        # старый формат — просто список строк, без источников
        self.chunks = [r if isinstance(r, str) else r["text"] for r in raw]
        self.sources = ["" if isinstance(r, str) else r.get("source", "") for r in raw]
        self._emb = np.load(epath)
        self._fit_bm25()
        return True

    def search(self, query: str, query_emb: list[float], k: int = 5) -> list[dict]:
        """Гибридный поиск: косинус по эмбеддингам + BM25, слияние через RRF.
        Возвращает [{"text", "source"}]."""
        if not self.chunks:
            return []
        q = np.array(query_emb, dtype=np.float32)
        emb = self._emb
        denom = (np.linalg.norm(emb, axis=1) * (np.linalg.norm(q) or 1e-9)) + 1e-9
        cos = emb @ q / denom
        bm = np.array(self._bm25.get_scores(tokenize(query) or ["_"]))
        # reciprocal rank fusion: ранги сопоставимы, а сырые скоры косинуса и BM25 — нет
        score = rrf_scores((np.argsort(-cos), np.argsort(-bm)), len(self.chunks))
        top = np.argsort(-score, kind="stable")[:k]
        return [{"text": self.chunks[i], "source": self.sources[i]} for i in top]


def recall_at_k(index: KBIndex, cases: list[dict], query_embs: list[list[float]], k: int = 5):
    """Доля вопросов, для которых в top-k нашёлся нужный фрагмент.

    case = {"question": ..., "expected": ...}; expected — подстрока, которую ищем
    в источнике или в тексте фрагмента. Возвращает (recall, список промахов)."""
    misses = []
    for case, emb in zip(cases, query_embs):
        exp = case["expected"].lower()
        hits = index.search(case["question"], emb, k=k)
        if not any(exp in h["source"].lower() or exp in h["text"].lower() for h in hits):
            misses.append(case["question"])
    return (1 - len(misses) / len(cases)) if cases else 0.0, misses


async def crawl(base_url: str, email: str, password: str,
                max_pages: int = 300) -> list[tuple[str, str]]:
    """Обход JSON-API платформы (Bearer-токен). Возвращает (источник, markdown):
    статьи /knowledge, описания спринтов, контент уроков."""
    import httpx

    api = base_url.rstrip("/") + "/api"
    docs: list[tuple[str, str]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{api}/auth/login", json={"email": email, "password": password}
            )
            if r.status_code != 200:
                return []
            token = r.json().get("token")
        except httpx.HTTPError:
            return []
        if not token:
            return []
        headers = {"Authorization": f"Bearer {token}"}

        async def get(path: str):
            try:
                rr = await client.get(api + path, headers=headers)
                return rr.json() if rr.status_code == 200 else None
            except httpx.HTTPError:
                return None

        for art in (await get("/knowledge")) or []:
            if art.get("content"):
                docs.append((f"База знаний «{art.get('title', '')}»",
                             f"# {art.get('title', '')}\n\n{art['content']}"))

        for sp in (await get("/sprints")) or []:
            sprint = f"Спринт «{sp.get('title', '')}»"
            parts = [f"# Спринт: {sp.get('title', '')}"]
            for key in ("description", "theory_desc", "practice_desc"):
                if sp.get(key):
                    parts.append(str(sp[key]))
            if len(parts) > 1:
                docs.append((sprint, "\n\n".join(parts)))
            for lesson in (await get(f"/sprints/{sp['id']}/lessons")) or []:
                full = (await get(f"/lessons/{lesson['id']}")) or lesson
                if full.get("content"):
                    docs.append((f"{sprint} → урок «{full.get('title', '')}»",
                                 f"# {full.get('title', '')}\n\n{full['content']}"))
                if len(docs) >= max_pages:
                    return docs
    return docs
