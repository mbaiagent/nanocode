"""nanocode —— 一个用来学习 Agent 与 Claude Code 核心架构的迷你编程 Agent。

整个包只依赖 Python 标准库，阅读顺序建议：

    messages.py  → 统一的消息格式（Agent 的"内存"长什么样）
    llm/         → 如何把统一格式翻译成各家 API
    tools/       → 工具是什么、怎么描述给模型
    agent.py     → 核心循环：模型 → 工具 → 模型 → ……
    permissions.py / hooks.py / context.py / mcp.py → 围绕循环的"安全带"与"扩展口"
"""

__version__ = "0.1.0"
