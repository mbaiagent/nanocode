"""Mock provider —— 不需要 API Key 的"假模型"，用于离线学习和自动化测试。

它不懂语言，只按剧本行动，但会产生和真实模型完全相同结构的输出
（文本块 + tool_use 块 + stop_reason + usage），所以 Agent 循环、权限、
hooks、Web 调试面板都能被完整地演示出来。

剧本（每次用户提问都会重新走一遍）：
  第 1 步：写待办清单 + 用 glob 看看项目结构         （演示：一次返回多个工具调用）
  第 2 步：写一个 demo 文件                           （演示：写操作触发权限确认）
  第 3 步：用 bash 运行它                              （演示：执行命令、把输出喂回模型）
  第 4 步：更新待办 + 给出最终回答（不再调用工具）      （演示：循环结束条件）

如果提问里包含"子agent / subagent / 调研"，第 1 步会改成调用 task 工具，演示子 Agent。
"""

from __future__ import annotations

import time
import uuid

from ..messages import LLMResponse, Usage, estimate_tokens, is_tool_result_message, text_block, text_of, tool_use_block
from .base import Provider, TextCallback


class MockProvider(Provider):
    name = "mock"

    def __init__(self, model: str = "mock-demo", delay: float = 0.02, **kwargs):
        super().__init__(model, **kwargs)
        self.delay = delay
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"toolu_mock_{self._n:03d}"

    def _stream(self, text: str, on_text: TextCallback) -> None:
        if not on_text:
            return
        for i in range(0, len(text), 6):
            on_text(text[i:i + 6])
            if self.delay:
                time.sleep(self.delay)

    def complete(self, system, messages, tools, max_tokens=4096, on_text: TextCallback = None) -> LLMResponse:
        tool_names = {t["name"] for t in tools}
        blocks = self._decide(messages, tool_names)
        for b in blocks:
            if b["type"] == "text":
                self._stream(b["text"], on_text)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        usage = Usage(estimate_tokens(system) + estimate_tokens(messages) + estimate_tokens(tools),
                      estimate_tokens(blocks), estimated=True)
        return LLMResponse(content=blocks, stop_reason=stop, usage=usage)

    # ------------------------------------------------------------------ 剧本

    def _decide(self, messages, tool_names) -> list:
        if not tool_names:  # 没有工具 = 被用来做"上下文压缩"的总结调用
            n_turns = sum(1 for m in messages if m["role"] == "user")
            return [text_block(f"【对话摘要】用户和助手进行了约 {n_turns} 轮交互，"
                               "助手创建并运行了 demo 文件。当前没有未完成的任务。")]

        # 找到最近一条"人说的话"，以及它之后已经发生了几步工具调用
        start = max(i for i, m in enumerate(messages) if m["role"] == "user" and not is_tool_result_message(m))
        prompt = text_of(messages[start])
        results = [b for m in messages[start + 1:] if is_tool_result_message(m) for b in m["content"]]
        step = sum(1 for m in messages[start + 1:] if is_tool_result_message(m))

        if results and results[-1].get("is_error"):
            return [text_block(f"上一步工具返回了错误：{results[-1]['content'][:200]}\n"
                               "（Mock 模型不会自动修复，真实模型会读这条错误信息并换一种做法。）")]

        is_sub = "task" not in tool_names  # 子 Agent 拿不到 task 工具
        if is_sub:
            if step == 0:
                return [text_block("我是子 Agent，先快速扫描一下目录。"),
                        tool_use_block(self._id(), "glob", {"pattern": "*"})]
            return [text_block(f"调研结论：目录里大约有 {results[-1]['content'].count(chr(10)) + 1} 个条目。"
                               "（子 Agent 只把这句结论交回主 Agent，扫描细节留在子 Agent 自己的上下文里。）")]

        wants_sub = any(k in prompt.lower() for k in ("子agent", "子 agent", "subagent", "调研"))
        todos = lambda s1, s2, s3: {"todos": [  # noqa: E731
            {"content": "了解项目结构", "status": s1},
            {"content": "编写 demo 文件", "status": s2},
            {"content": "运行并验证", "status": s3}]}

        if step == 0:
            if wants_sub:
                return [text_block("这个任务适合交给子 Agent 去调研，它有独立的上下文。"),
                        tool_use_block(self._id(), "task",
                                       {"description": "调研项目结构", "prompt": "看看当前目录有哪些文件，给我一句话结论"})]
            return [text_block("好的。我先列一个待办清单，同时看看项目里有什么文件。"),
                    tool_use_block(self._id(), "todo_write", todos("in_progress", "pending", "pending")),
                    tool_use_block(self._id(), "glob", {"pattern": "*"})]
        if step == 1:
            safe = prompt.replace("\\", "\\\\").replace('"""', "'''")[:200]
            code = (f'"""由 nanocode 的 Mock 模型生成的演示文件。"""\n\n'
                    f'request = """{safe}"""\n\n'
                    'print("你好，我是 nanocode！")\n'
                    'print(f"你的请求是：{request}")\n'
                    'print(f"请求长度：{len(request)} 个字符")\n')
            # 每次用新文件名：覆盖已有文件需要先 read_file，Mock 剧本里没有这一步。
            # 只用时间戳不够：同一秒内运行两次会撞名（验证时真的遇到过），所以再加随机后缀
            path = f"nanocode_demo/hello_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}.py"
            return [text_block("项目结构看完了。接下来写一个 demo 文件。"),
                    tool_use_block(self._id(), "todo_write", todos("completed", "in_progress", "pending")),
                    tool_use_block(self._id(), "write_file", {"path": path, "content": code})]
        if step == 2:
            path = next((b["input"]["path"] for m in messages[start:] if m["role"] == "assistant"
                         for b in m["content"] if b["type"] == "tool_use" and b["name"] == "write_file"),
                        "nanocode_demo/hello.py")
            return [text_block("文件写好了，运行一下验证结果。"),
                    tool_use_block(self._id(), "todo_write", todos("completed", "completed", "in_progress")),
                    tool_use_block(self._id(), "bash", {"command": f"python3 {path}"})]
        if step == 3:
            return [tool_use_block(self._id(), "todo_write", todos("completed", "completed", "completed"))]
        bash_ids = {b["id"] for m in messages[start:] if m["role"] == "assistant"
                    for b in m["content"] if b["type"] == "tool_use" and b["name"] == "bash"}
        output = next((r["content"].strip() for r in reversed(results) if r["tool_use_id"] in bash_ids), "")
        return [text_block("全部完成 ✅\n\n运行输出：\n" + output[:500] + "\n\n"
                           "（这是 Mock 模型的固定剧本。换成真实模型后，每一步做什么都由模型自己决定。）")]
