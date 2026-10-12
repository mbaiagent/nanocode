"""RAG（检索增强生成）：给 Agent 一个"按语义/关键词搜文档"的能力。

为什么编程场景默认只用 grep，而这里还要做 RAG？
  - 代码：标识符是精确的，grep 永远最新、最准，不需要索引。
  - 文档 / 知识库：用户用自然语言提问（"怎么保护主上下文"），文档里却写的是
    "子 Agent 独立上下文"。词对不上，grep 就找不到，这时才需要检索。

本文件从零实现一个小而完整的检索管线，只用标准库：

    文件 ──▶ chunk_file（切块）──▶ tokenize（分词）──▶ BM25 索引  ─┐
                                    └▶ Embedder（可选，向量）──────┴▶ RRF 融合 ──▶ top-k

  1. 切块：Markdown 按标题切，其余按行窗口切。块太大检索不准，太小丢上下文。
  2. BM25：经典的关键词打分，考虑词频、词的稀有程度（IDF）、文档长度。
     中文没有空格，这里用"字的二元组（bigram）"当词，不需要分词库。
  3. 向量检索（可选）：调用任意 OpenAI 兼容的 /embeddings 接口，余弦相似度排序。
  4. 混合检索：BM25 和向量各排一个名次，用 RRF（倒数排名融合）合并，
     不需要调权重，对两种分数的量纲差异不敏感。
  5. 增量索引：按文件的 mtime + size 判断变化，只重新切块 / 重新算向量变化的文件。
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .tools.search import glob_to_regex, walk_files

DEFAULT_INCLUDE = ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.rst"]
INDEX_VERSION = 1

# --------------------------------------------------------------------------- 分词

_CJK = re.compile(r"[㐀-鿿]+")
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(text: str) -> list[str]:
    """英文/数字按单词；中文按相邻两字（bigram），单个孤字保留为 unigram。

    例："edit_file 必须唯一" → ["edit", "file", "必须", "须唯", "唯一"]
    查询和文档用同一个分词函数，这是检索能对上的前提。
    """
    text = _CAMEL.sub(" ", text)
    tokens: list[str] = []
    pos = 0
    for m in _CJK.finditer(text):
        tokens += _WORD.findall(text[pos:m.start()].lower())
        run = m.group()
        tokens += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
        pos = m.end()
    tokens += _WORD.findall(text[pos:].lower())
    return tokens


# --------------------------------------------------------------------------- 切块

@dataclass
class Chunk:
    path: str
    start: int          # 起始行（1 起）
    end: int            # 结束行
    heading: str
    text: str
    vec: Optional[list[float]] = None

    @property
    def index_text(self) -> str:
        """参与检索的文本：标题 + 正文。标题往往概括了这一块讲什么。"""
        return f"{self.heading}\n{self.text}" if self.heading else self.text


def chunk_file(path: str, text: str, max_chars: int = 900) -> list[Chunk]:
    """把一个文件切成块。

    Markdown：遇到标题就开新块（记住当前标题），块超过 max_chars 就在空行处再切。
    其他文本：按空行分段，攒到 max_chars 就切。
    不做 overlap：块边界落在标题/空行上，语义本来就是完整的。
    """
    is_md = path.lower().endswith((".md", ".markdown"))
    lines = text.splitlines()
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_start = 1
    heading = ""
    in_fence = False

    def flush(end: int) -> None:
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(path, buf_start, end, heading, body))

    size = 0
    for no, line in enumerate(lines, 1):
        if is_md and line.lstrip().startswith("```"):
            in_fence = not in_fence
        is_heading = is_md and not in_fence and re.match(r"#{1,4}\s+\S", line)
        if is_heading and buf:
            flush(no - 1)
            buf, size, buf_start = [], 0, no
        if is_heading:
            heading = line.lstrip("# ").strip()
        elif not buf:
            buf_start = no
        buf.append(line)
        size += len(line) + 1
        if size >= max_chars and not in_fence and not line.strip():
            flush(no)
            buf, size, buf_start = [], 0, no + 1
        elif size >= max_chars * 2 and not in_fence:   # 没有空行的超长段落，硬切
            flush(no)
            buf, size, buf_start = [], 0, no + 1
    if buf:
        flush(len(lines))
    return chunks


# --------------------------------------------------------------------------- BM25

class BM25:
    """BM25 打分：  score = Σ IDF(词) · tf·(k1+1) / (tf + k1·(1-b + b·文档长/平均长))

    - tf：词在块里出现几次（出现越多越相关，但边际递减，由 k1 控制）
    - IDF：词越稀有越重要（"的"几乎每块都有，IDF 接近 0）
    - b：长块的惩罚强度，避免长块仅因包含更多词就占便宜
    """

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: list[dict[str, int]] = []
        self.len = [len(d) for d in docs]
        self.df: dict[str, int] = {}
        self.postings: dict[str, list[int]] = {}
        for i, doc in enumerate(docs):
            counts: dict[str, int] = {}
            for t in doc:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                self.df[t] = self.df.get(t, 0) + 1
                self.postings.setdefault(t, []).append(i)
        self.n = len(docs)
        self.avg = (sum(self.len) / self.n) if self.n else 0.0

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query_tokens: list[str], k: int = 10) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for term in set(query_tokens):
            idf = self.idf(term)
            for i in self.postings.get(term, ()):
                tf = self.tf[i][term]
                norm = 1 - self.b + self.b * self.len[i] / (self.avg or 1)
                scores[i] = scores.get(i, 0.0) + idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        return sorted(scores.items(), key=lambda x: -x[1])[:k]


# --------------------------------------------------------------------------- 向量

class EmbeddingError(Exception):
    pass


class Embedder:
    """OpenAI 兼容的 /embeddings 接口（OpenAI、千问、智谱、硅基流动、Ollama……都支持）。"""

    def __init__(self, model: str, base_url: str, api_key: str = "", batch: int = 32, timeout: float = 60):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.batch, self.timeout = batch, timeout

    @property
    def name(self) -> str:
        return f"{self.base_url}|{self.model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out += self._call(texts[i:i + self.batch])
        return out

    def _call(self, texts: list[str]) -> list[list[float]]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body = json.dumps({"model": self.model, "input": [t[:6000] for t in texts]}).encode()
        req = urllib.request.Request(f"{self.base_url}/embeddings", body, headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise EmbeddingError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise EmbeddingError(f"网络错误：{e}") from None
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        if len(items) != len(texts):
            raise EmbeddingError(f"返回了 {len(items)} 个向量，期望 {len(texts)} 个")
        return [d["embedding"] for d in items]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def rrf(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """倒数排名融合：每个榜单里排第 r 名的得 1/(k+r) 分，各榜单得分相加。

    不用关心 BM25 分数（可能是 0~30）和余弦相似度（0~1）量纲不同。
    只要在多个榜单里都靠前，就会排到最前面。
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, i in enumerate(ranking, 1):
            scores[i] = scores.get(i, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


# --------------------------------------------------------------------------- 索引

@dataclass
class Hit:
    chunk: Chunk
    score: float


class DocIndex:
    def __init__(self, root: str, include: list[str] | None = None, embedder: Embedder | None = None,
                 store: str | None = None, max_chars: int = 900):
        self.root = os.path.abspath(root)
        self.include = [glob_to_regex(p) for p in (include or DEFAULT_INCLUDE)]
        self.include_src = include or DEFAULT_INCLUDE
        self.embedder = embedder
        self.store = store
        self.max_chars = max_chars
        self.files: dict[str, dict] = {}      # 相对路径 -> {"sig": [mtime_ns, size], "chunks": [Chunk]}
        self.chunks: list[Chunk] = []
        self.bm25: BM25 | None = None
        self.warning = ""
        self._load()

    # ---- 持久化

    def _load(self) -> None:
        if not self.store or not os.path.isfile(self.store):
            return
        try:
            with open(self.store, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != INDEX_VERSION or data.get("max_chars") != self.max_chars:
                return
            reuse_vec = data.get("embedder") == (self.embedder.name if self.embedder else None)
            for rel, entry in data["files"].items():
                self.files[rel] = {"sig": entry["sig"], "chunks": [
                    Chunk(rel, c[0], c[1], c[2], c[3], c[4] if reuse_vec else None) for c in entry["chunks"]]}
        except (OSError, ValueError, KeyError, IndexError):
            self.files = {}   # 索引损坏：丢掉，重新建

    def save(self) -> None:
        if not self.store:
            return
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        data = {"version": INDEX_VERSION, "max_chars": self.max_chars,
                "embedder": self.embedder.name if self.embedder else None,
                "files": {rel: {"sig": e["sig"], "chunks": [
                    [c.start, c.end, c.heading, c.text, [round(x, 5) for x in c.vec] if c.vec else None]
                    for c in e["chunks"]]} for rel, e in self.files.items()}}
        tmp = self.store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.store)

    # ---- 构建 / 增量更新

    def _scan(self) -> dict[str, list[int]]:
        found = {}
        for path in walk_files(self.root):
            rel = os.path.relpath(path, self.root).replace(os.sep, "/")
            if any(rx.match(rel) for rx in self.include):
                st = os.stat(path)
                if st.st_size <= 2_000_000:
                    found[rel] = [st.st_mtime_ns, st.st_size]
        return found

    def refresh(self) -> dict:
        """让索引和磁盘一致。返回 {"added","changed","removed","embedded"} 的数量。"""
        scan = self._scan()
        stats = {"added": 0, "changed": 0, "removed": 0, "embedded": 0}
        for rel in set(self.files) - set(scan):
            del self.files[rel]
            stats["removed"] += 1
        for rel, sig in scan.items():
            old = self.files.get(rel)
            if old and old["sig"] == sig:
                continue
            try:
                with open(os.path.join(self.root, rel), encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            stats["changed" if old else "added"] += 1
            self.files[rel] = {"sig": sig, "chunks": chunk_file(rel, text, self.max_chars)}
        self.chunks = [c for e in self.files.values() for c in e["chunks"]]
        self.warning = ""
        if self.embedder:
            missing = [c for c in self.chunks if c.vec is None]
            if missing:
                try:
                    for c, v in zip(missing, self.embedder.embed([c.index_text for c in missing])):
                        c.vec = v
                    stats["embedded"] = len(missing)
                except EmbeddingError as e:
                    self.warning = f"向量化失败，本次只用关键词检索：{e}"
        self.bm25 = BM25([tokenize(c.index_text) for c in self.chunks])
        if any(stats[k] for k in ("added", "changed", "removed", "embedded")):
            self.save()
        return stats

    # ---- 检索

    def search(self, query: str, k: int = 5, mode: str = "auto") -> list[Hit]:
        """mode: bm25 | vector | hybrid | auto（有向量就混合，否则 bm25）"""
        if self.bm25 is None:
            self.refresh()
        if not self.chunks:
            return []
        have_vec = bool(self.embedder) and all(c.vec is not None for c in self.chunks)
        if mode == "auto":
            mode = "hybrid" if have_vec else "bm25"
        if mode in ("vector", "hybrid") and not have_vec:
            mode = "bm25"
        pool = max(k * 4, 20)
        bm = self.bm25.search(tokenize(query), pool) if mode in ("bm25", "hybrid") else []
        vec: list[tuple[int, float]] = []
        if mode in ("vector", "hybrid"):
            try:
                q = self.embedder.embed([query])[0]
                vec = sorted(((i, cosine(q, c.vec)) for i, c in enumerate(self.chunks)), key=lambda x: -x[1])[:pool]
            except EmbeddingError as e:
                self.warning = f"查询向量化失败，改用关键词检索：{e}"
                mode = "bm25"
        if mode == "bm25":
            ranked = bm
        elif mode == "vector":
            ranked = vec
        else:
            ranked = rrf([[i for i, _ in bm], [i for i, _ in vec]])
        return [Hit(self.chunks[i], s) for i, s in ranked[:k]]


def make_embedder(cfg: dict | None) -> Embedder | None:
    """从配置创建 Embedder。cfg 例：{"provider": "qwen", "model": "text-embedding-v3"}
    或 {"base_url": "https://xxx/v1", "model": "...", "api_key_env": "MY_KEY"}。"""
    if not cfg or not cfg.get("model"):
        return None
    from .llm import PRESETS
    preset = PRESETS.get(cfg.get("provider", ""), {})
    base_url = cfg.get("base_url") or preset.get("base_url", "")
    if not base_url:
        return None
    key_env = cfg.get("api_key_env") or preset.get("key_env") or ""
    api_key = cfg.get("api_key") or (os.environ.get(key_env, "") if key_env else "") or os.environ.get("NANOCODE_API_KEY", "")
    return Embedder(cfg["model"], base_url, api_key)
