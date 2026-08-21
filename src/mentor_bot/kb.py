import json
import os
import re
import numpy as np


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


class KBIndex:
    def __init__(self, path: str):
        self.path = path
        self.chunks: list[str] = []
        self._emb: np.ndarray | None = None
        self._bm25 = None

    def _fit_bm25(self):
        if not self.chunks:
            self._bm25 = None
            return
        from rank_bm25 import BM25Okapi
        self._bm25 = BM25Okapi([c.lower().split() for c in self.chunks])

    def build(self, chunks: list[str], embeddings: list[list[float]]):
        os.makedirs(self.path, exist_ok=True)
        self.chunks = chunks
        self._emb = np.array(embeddings, dtype=np.float32)
        with open(os.path.join(self.path, "chunks.json"), "w") as f:
            json.dump(chunks, f, ensure_ascii=False)
        np.save(os.path.join(self.path, "emb.npy"), self._emb)
        self._fit_bm25()

    def load(self) -> bool:
        cpath = os.path.join(self.path, "chunks.json")
        epath = os.path.join(self.path, "emb.npy")
        if not (os.path.exists(cpath) and os.path.exists(epath)):
            return False
        with open(cpath) as f:
            self.chunks = json.load(f)
        self._emb = np.load(epath)
        self._fit_bm25()
        return True

    def search(self, query: str, query_emb: list[float], k: int = 5) -> list[str]:
        if not self.chunks:
            return []
        q = np.array(query_emb, dtype=np.float32)
        emb = self._emb
        denom = (np.linalg.norm(emb, axis=1) * (np.linalg.norm(q) or 1e-9)) + 1e-9
        cos = emb @ q / denom
        bm = np.array(self._bm25.get_scores(query.lower().split()))
        # rank fusion: сумма обратных рангов
        order_cos = np.argsort(-cos)
        order_bm = np.argsort(-bm)
        score = np.zeros(len(self.chunks))
        for rank, i in enumerate(order_cos):
            score[i] += 1.0 / (rank + 1)
        for rank, i in enumerate(order_bm):
            score[i] += 1.0 / (rank + 1)
        top = np.argsort(-score)[:k]
        return [self.chunks[i] for i in top]


async def crawl(base_url: str, email: str, password: str, max_pages: int = 300) -> list[str]:
    """Обход JSON-API платформы (Bearer-токен). Возвращает markdown-документы:
    статьи /knowledge, описания спринтов, контент уроков."""
    import httpx

    api = base_url.rstrip("/") + "/api"
    docs: list[str] = []
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
                docs.append(f"# {art.get('title', '')}\n\n{art['content']}")

        for sp in (await get("/sprints")) or []:
            parts = [f"# Спринт: {sp.get('title', '')}"]
            for key in ("description", "theory_desc", "practice_desc"):
                if sp.get(key):
                    parts.append(str(sp[key]))
            if len(parts) > 1:
                docs.append("\n\n".join(parts))
            for lesson in (await get(f"/sprints/{sp['id']}/lessons")) or []:
                full = (await get(f"/lessons/{lesson['id']}")) or lesson
                if full.get("content"):
                    docs.append(f"# {full.get('title', '')}\n\n{full['content']}")
                if len(docs) >= max_pages:
                    return docs
    return docs
