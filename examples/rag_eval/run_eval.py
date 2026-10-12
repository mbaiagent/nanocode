"""检索评测：同一批问题，对比几种找文档的办法。

运行：
    python3 examples/rag_eval/run_eval.py                  # grep 基线 vs BM25
    python3 examples/rag_eval/run_eval.py --verbose        # 逐题显示每种方法排第一的文件
    python3 examples/rag_eval/run_eval.py --embed-provider qwen --embed-model text-embedding-v3
                                                           # 再加上 向量 / 混合 两行（需要 API Key）

指标（按"文件"粒度，命中 expected 里任意一个即算对）：
    recall@k   前 k 个结果里有正确文档的问题占比
    MRR        正确文档排名的倒数的平均值（第 1 名得 1，第 2 名得 0.5……）

方法：
    grep-原句   把整句问题当字符串去 grep。Agent 偶尔会这样做，通常一无所获。
    grep-关键词 把问题拆成关键词（和 BM25 用同一个分词），任一词命中的行都算，按命中行数给文件排序。
                这是对 grep 的"最大善意"模拟：没有词的稀有度（IDF）、没有长度归一化，也没有分块。
    bm25        nanocode/rag.py 里的 BM25（分块后取每个文件得分最高的块）。

注意：这是 22 道题的小评测，用来演示"怎么评"，不是对某种方法的定论。
题目少，几题的差别就会让百分比变化很大；换语料、换题目，结论可能不同。
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from nanocode.rag import DocIndex, make_embedder, tokenize  # noqa: E402
from nanocode.tools.search import walk_files  # noqa: E402


def load_corpus(root):
    corpus = {}
    for path in walk_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel.endswith(".md") and rel.startswith("docs/"):
            with open(path, encoding="utf-8") as f:
                corpus[rel] = f.read().splitlines()
    return corpus


def grep_phrase(corpus, q):
    hits = {rel: sum(q in line for line in lines) for rel, lines in corpus.items()}
    return [rel for rel, n in sorted(hits.items(), key=lambda x: -x[1]) if n]


def grep_keywords(corpus, q):
    terms = sorted(set(tokenize(q)))
    rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    hits = {rel: sum(bool(rx.search(line)) for line in lines) for rel, lines in corpus.items()}
    return [rel for rel, n in sorted(hits.items(), key=lambda x: -x[1]) if n]


def by_index(index, mode):
    def run(q):
        files = []
        for h in index.search(q, 30, mode=mode):
            if h.chunk.path not in files:
                files.append(h.chunk.path)
        return files
    return run


def evaluate(name, fn, questions, ks=(1, 3, 5)):
    ranks = []
    for item in questions:
        files = fn(item["q"])
        rank = next((i + 1 for i, f in enumerate(files) if f in item["expected"]), None)
        ranks.append((rank, files[:1]))
    n = len(questions)
    row = {f"recall@{k}": sum(1 for r, _ in ranks if r and r <= k) / n for k in ks}
    row["MRR"] = sum(1 / r for r, _ in ranks if r) / n
    return name, row, ranks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT, help="语料所在的项目根目录（默认本项目）")
    ap.add_argument("--questions", default=os.path.join(os.path.dirname(__file__), "questions.json"))
    ap.add_argument("--embed-provider", default="")
    ap.add_argument("--embed-model", default="")
    ap.add_argument("--embed-base-url", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    with open(args.questions, encoding="utf-8") as f:
        questions = json.load(f)["questions"]
    corpus = load_corpus(args.root)
    index = DocIndex(args.root, include=["docs/*.md"])
    index.refresh()
    print(f"语料：{len(corpus)} 篇文档，{len(index.chunks)} 个块；问题：{len(questions)} 道\n")

    methods = [("grep-原句", lambda q: grep_phrase(corpus, q)),
               ("grep-关键词", lambda q: grep_keywords(corpus, q)),
               ("bm25", by_index(index, "bm25"))]
    emb = make_embedder({"provider": args.embed_provider, "model": args.embed_model, "base_url": args.embed_base_url}) \
        if args.embed_model else None
    if emb:
        vindex = DocIndex(args.root, include=["docs/*.md"], embedder=emb)
        vindex.refresh()
        if vindex.warning:
            print("向量化失败，跳过向量/混合：", vindex.warning, "\n")
        else:
            methods += [("vector", by_index(vindex, "vector")), ("hybrid", by_index(vindex, "hybrid"))]

    results = [evaluate(name, fn, questions) for name, fn in methods]
    print(f"{'方法':<12}{'recall@1':>10}{'recall@3':>10}{'recall@5':>10}{'MRR':>8}")
    for name, row, _ in results:
        print(f"{name:<12}{row['recall@1']:>10.0%}{row['recall@3']:>10.0%}{row['recall@5']:>10.0%}{row['MRR']:>8.2f}")

    if args.verbose:
        print("\n逐题（✓ 第 1 名正确 / 数字 = 正确文档的排名 / ✗ 没找到）")
        for i, item in enumerate(questions):
            cells = []
            for name, _, ranks in results:
                r = ranks[i][0]
                cells.append(f"{name}:{'✓' if r == 1 else (r or '✗')}")
            print(f"  {item['q']}\n    " + "  ".join(cells))


if __name__ == "__main__":
    main()
