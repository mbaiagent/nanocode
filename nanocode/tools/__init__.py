"""内置工具集。"""

from .base import Tool, ToolContext, ToolError, ToolRegistry, ToolResult, truncate
from .files import EditFile, ReadFile, WriteFile
from .planning import Skill, Task, TodoWrite
from .rag_tool import SearchDocs
from .search import Glob, Grep
from .shell import Bash
from .web import WebFetch
from .web_search import WebSearch


def default_tools() -> list[Tool]:
    tools = [ReadFile(), WriteFile(), EditFile(), Bash(), Glob(), Grep(), SearchDocs(), WebFetch(), WebSearch(),
             TodoWrite(), Task(), Skill()]
    # 没配置好的可选工具不要出现在工具列表里：模型看到就会调，然后白拿一句"未配置"。
    # WebSearch 默认 duckduckgo 可用；切到 tavily/brave/serper/searxng 但没填 Key/地址时才会被过滤掉。
    return [t for t in tools if getattr(t, "available", True)]


__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "ToolResult", "truncate", "default_tools",
           "ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep", "SearchDocs", "WebFetch", "WebSearch", "TodoWrite", "Task", "Skill"]
