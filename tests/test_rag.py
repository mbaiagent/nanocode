"""RAG 测试：分词、切块、BM25、向量与混合检索、增量索引、工具。"""

import json
import os
import time
import unittest

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from helpers import FakeSSEServer, Workspace  # noqa: E402

from nanocode.rag import BM25, DocIndex, Embedder, EmbeddingError, chunk_file, cosine, rrf, tokenize  # noqa: E402
from nanocode.tools import SearchDocs, ToolContext  # noqa: E402

MD = """# 总览

这里是总览。

## 权限

default 模式下写操作要确认。

## 子 Agent

子 Agent 拥有独立的上下文，只把结论交回主 Agent。

```python
# 这个注释不是标题
x = 1
```
"""


class FakeEmbedder:
    """用关键词出现与否当作"向量"：[含 agent, 含 权限]。不需要网络。"""

    name = "fake"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return [[1.0 if "agent" in t.lower() else 0.0, 1.0 if "权限" in t else 0.0, 0.1] for t in texts]


class BrokenEmbedder:
    """永远失败的 embedder：模拟 key 错 / 服务挂。"""

    name = "broken"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        raise EmbeddingError("401 Unauthorized")


class TokenizeTest(unittest.TestCase):
    def test_mixed(self):
        self.assertEqual(tokenize("edit_file 必须唯一"), ["edit", "file", "必须", "须唯", "唯一"])
        self.assertEqual(tokenize("camelCaseName"), ["camel", "case", "name"])
        self.assertEqual(tokenize("a子b"), ["a", "子", "b"])


class ChunkTest(unittest.TestCase):
    def test_markdown_split_by_heading_and_fence(self):
        chunks = chunk_file("a.md", MD)
        self.assertEqual([c.heading for c in chunks], ["总览", "权限", "子 Agent"])
        self.assertIn("这个注释不是标题", chunks[-1].text)       # 代码块里的 # 不算标题
        self.assertEqual((chunks[1].start, chunks[1].end), (5, 8))

    def test_long_paragraph_is_split(self):
        text = "\n".join(f"第 {i} 行 " + "字" * 50 for i in range(100))
        chunks = chunk_file("a.txt", text, max_chars=500)
        self.assertGreater(len(chunks), 5)
        self.assertEqual(sum(c.text.count("\n") + 1 for c in chunks), 100)   # 没有丢行


class RankingTest(unittest.TestCase):
    def test_bm25_prefers_rare_terms_and_shorter_docs(self):
        docs = [tokenize(t) for t in ["the cat sat", "the dog barked loudly at the mailman", "cat cat cat"]]
        bm = BM25(docs)
        top = [i for i, _ in bm.search(tokenize("cat"), 3)]
        self.assertEqual(top[0], 2)
        self.assertEqual(set(top), {0, 2})
        self.assertEqual(bm.search(tokenize("zebra"), 3), [])

    def test_rrf_rewards_agreement(self):
        fused = [i for i, _ in rrf([[1, 2, 3], [4, 2, 5]])]
        self.assertEqual(fused[0], 2)    # 两个榜单里都出现，哪怕都不是第一名

    def test_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([0, 0], [1, 1]), 0.0)


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.ws.write("docs/perm.md", "# 权限\n\n写文件之前需要用户确认，plan 模式只读。\n")
        self.ws.write("docs/sub.md", "# 子 Agent\n\n子 Agent 拥有独立的上下文，只返回结论。\n")
        self.ws.write("src/code.py", "def permission(): pass\n")          # 不在默认 include 里
        self.store = os.path.join(self.ws.dir, ".nanocode", "index", "docs.json")

    def tearDown(self):
        self.ws.cleanup()

    def test_search_and_include(self):
        idx = DocIndex(self.ws.dir)
        stats = idx.refresh()
        self.assertEqual(stats["added"], 2)
        hits = idx.search("独立上下文", 2)
        self.assertEqual(hits[0].chunk.path, "docs/sub.md")
        self.assertEqual(idx.search("plan 只读", 1)[0].chunk.path, "docs/perm.md")

    def test_incremental_and_persistence(self):
        emb = FakeEmbedder()
        idx = DocIndex(self.ws.dir, embedder=emb, store=self.store)
        idx.refresh()
        self.assertEqual(emb.calls, 2)
        # 重新加载：文件没变，不应该重新向量化
        idx2 = DocIndex(self.ws.dir, embedder=emb, store=self.store)
        self.assertEqual(idx2.refresh(), {"added": 0, "changed": 0, "removed": 0, "embedded": 0})
        self.assertEqual(emb.calls, 2)
        # 改一个文件、删一个文件
        time.sleep(0.01)
        self.ws.write("docs/perm.md", "# 权限\n\n全新的内容。\n")
        os.remove(os.path.join(self.ws.dir, "docs/sub.md"))
        stats = idx2.refresh()
        self.assertEqual((stats["changed"], stats["removed"], stats["embedded"]), (1, 1, 1))
        self.assertEqual(emb.calls, 3)
        self.assertEqual(idx2.search("全新", 1)[0].chunk.path, "docs/perm.md")

    def test_embedder_change_invalidates_vectors(self):
        DocIndex(self.ws.dir, embedder=FakeEmbedder(), store=self.store).refresh()
        other = FakeEmbedder()
        other.name = "another-model"
        DocIndex(self.ws.dir, embedder=other, store=self.store).refresh()
        self.assertEqual(other.calls, 2)    # 换了向量模型，旧向量不能复用

    def test_hybrid_uses_vectors(self):
        idx = DocIndex(self.ws.dir, embedder=FakeEmbedder())
        idx.refresh()
        hits = idx.search("agent", 2, mode="hybrid")
        self.assertEqual(hits[0].chunk.path, "docs/sub.md")
        self.assertEqual(len(idx.search("agent", 2, mode="vector")), 2)

    def test_embedding_failure_falls_back_to_bm25(self):
        class Broken(FakeEmbedder):
            def embed(self, texts):
                raise EmbeddingError("没网")
        idx = DocIndex(self.ws.dir, embedder=Broken())
        idx.refresh()
        self.assertIn("没网", idx.warning)
        self.assertEqual(idx.search("独立上下文", 1)[0].chunk.path, "docs/sub.md")

    def test_embedding_failure_opens_a_circuit_breaker(self):
        """#49：配了 embedder 但服务挂时，不能每次检索都重试全量向量化。"""
        emb = BrokenEmbedder()
        idx = DocIndex(self.ws.dir, embedder=emb)
        idx.refresh()
        self.assertEqual(emb.calls, 2)                            # 两个文件各一个 chunk
        # 连着检索几次：熔断期间一次都不该再调 embedder（以前每次都白等一次超时）
        for _ in range(3):
            idx.search("独立上下文", 1)
        self.assertEqual(emb.calls, 2)
        self.assertIn("冷却", idx.warning)                        # 说清楚是冷却，别让用户以为检索坏了
        # 把失败时刻推到很久以前：允许再试一次（这次仍然失败，但确实又试了）
        idx._embed_failed_at = time.time() - idx._embed_cooldown - 1
        idx.refresh()
        self.assertEqual(emb.calls, 4)

    def test_corrupt_index_is_ignored(self):
        os.makedirs(os.path.dirname(self.store))
        with open(self.store, "w") as f:
            f.write("{坏的")
        self.assertEqual(DocIndex(self.ws.dir, store=self.store).refresh()["added"], 2)


class EmbedderTest(unittest.TestCase):
    def test_http(self):
        resp = json.dumps({"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})
        srv = FakeSSEServer(resp)
        try:
            vecs = Embedder("m", srv.url + "/v1", "k").embed(["a", "b"])
        finally:
            srv.close()
        self.assertEqual(vecs, [[1, 0], [0, 1]])       # 按 index 排回原顺序
        req = srv.requests[0]
        self.assertEqual(req["path"], "/v1/embeddings")
        self.assertEqual(req["body"]["input"], ["a", "b"])

    def test_error(self):
        srv = FakeSSEServer('{"error": "bad"}', status=401)
        try:
            with self.assertRaises(EmbeddingError):
                Embedder("m", srv.url).embed(["a"])
        finally:
            srv.close()


class ToolTest(unittest.TestCase):
    def test_search_docs_tool(self):
        ws = Workspace()
        try:
            ws.write("README.md", "# 项目\n\n运行测试用 python3 -m unittest。\n")
            ctx = ToolContext(cwd=ws.dir)
            r = SearchDocs().execute({"query": "怎么运行测试"}, ctx)
            self.assertFalse(r.is_error, r.content)
            self.assertIn("README.md:1-3 [项目]", r.content)
            self.assertTrue(os.path.isfile(os.path.join(ws.dir, ".nanocode", "index", "docs.json")))
            self.assertTrue(SearchDocs().execute({"query": "  "}, ctx).is_error)
            self.assertIn("没有找到", SearchDocs().execute({"query": "zzzzqqqq"}, ctx).content)
        finally:
            ws.cleanup()

    def test_empty_workspace(self):
        ws = Workspace()
        try:
            r = SearchDocs().execute({"query": "x"}, ToolContext(cwd=ws.dir))
            self.assertIn("没有可检索的文档", r.content)
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
