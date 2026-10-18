"""search_docs 工具：让 Agent 按"意思"搜索文档 / 知识库。

和 grep 的分工写在 description 里，模型才知道什么时候该用哪个：
  grep        → 已知确切的标识符或字符串（函数名、报错信息）
  search_docs → 只知道想问什么，不知道文档里的原话（"怎么处理限流？"）
"""

from __future__ import annotations

import os

from .base import Tool, ToolContext, ToolError


class SearchDocs(Tool):
    name = "search_docs"
    is_read_only = True
    description = ("在项目文档（默认 .md / .txt / .rst，可配置）中按含义检索，返回最相关的片段，格式为"
                   "`文件:起止行 [所在标题] 得分` 加片段开头。适合用自然语言提问、不知道文档里确切用词时使用。"
                   "已知确切的函数名、变量名、报错文本时请改用 grep。需要完整内容再用 read_file 读对应行范围。")
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "自然语言问题或关键词，例如「子 Agent 如何避免污染上下文」"},
            "k": {"type": "integer", "description": "返回几条，默认 5，最多 10"},
        },
        "required": ["query"],
    }

    def permission_target(self, input):
        return ""   # 只读 + 范围固定在工作目录内，不需要按参数匹配规则

    def _index(self, ctx: ToolContext):
        index = ctx.state.get("doc_index")
        if index is None:
            from ..rag import DocIndex, make_embedder
            cfg = ctx.state.get("rag_config") or {}
            store = os.path.join(ctx.cwd, ".nanocode", "index", "docs.json")
            index = DocIndex(ctx.cwd, cfg.get("include"), make_embedder(cfg.get("embedding")), store)
            ctx.state["doc_index"] = index
        return index

    def run(self, input, ctx):
        query = input["query"].strip()
        if not query:
            raise ToolError("query 不能为空")
        k = max(1, min(int(input.get("k", 5)), 10))
        index = self._index(ctx)
        # cancel 传进去：全量向量化可能几十次 HTTP 调用，用户按停止必须能拦住（#49）
        index.refresh(ctx.cancel)
        if not index.chunks:
            return "没有可检索的文档（默认匹配 .md / .txt / .rst，可在 settings.json 的 rag.include 里配置）"
        hits = index.search(query, k, cancel=ctx.cancel)
        if not hits:
            return "没有找到相关片段。可以换几个关键词再试，或改用 grep。"
        out = []
        for h in hits:
            c = h.chunk
            snippet = " ".join(c.text.split())[:260]
            head = f" [{c.heading}]" if c.heading else ""
            out.append(f"{c.path}:{c.start}-{c.end}{head}  ({h.score:.3f})\n  {snippet}")
        note = f"\n\n注意：{index.warning}" if index.warning else ""
        return "\n\n".join(out) + note
